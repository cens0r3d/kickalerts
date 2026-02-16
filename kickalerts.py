import aiohttp
import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any

import discord
from redbot.core import commands, Config, checks
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import pagify, box
from redbot.core.utils.menus import menu, DEFAULT_CONTROLS

log = logging.getLogger("red.kickalerts")

KICK_API_BASE = "https://kick.com/api/v2/channels"
KICK_BASE_URL = "https://kick.com"
KICK_COLOR = 0x53FC18  # Kick's signature green


class KickAlerts(commands.Cog):
    """Monitor Kick.com streamers and post live announcements in Discord."""

    __version__ = "1.3.0"
    __author__ = "YourName"

    def __init__(self, bot: Red):
        self.bot = bot
        self.session: Optional[aiohttp.ClientSession] = None
        self.config = Config.get_conf(self, identifier=7274927492, force_registration=True)

        default_guild = {
            "streamers": {},
            # Structure per streamer:
            # "streamer_username": {
            #     "channel_id": int,          # Discord channel to post in
            #     "ping_role_id": int | None, # Role to ping
            #     "custom_message": str | None,
            #     "delete_after_offline": bool,
            #     "last_message_id": int | None,
            #     "is_live": bool,
            #     "last_stream_id": int | None,
            # }
            "global_channel_id": None,
            "global_ping_role_id": None,
            "check_interval": 60,  # seconds between checks
            "embed_style": "detailed",  # "detailed" or "minimal"
            "show_viewer_count": True,
            "show_category": True,
            "auto_delete": False,
        }

        self.config.register_guild(**default_guild)
        self._check_task: Optional[asyncio.Task] = None
        self._ready = asyncio.Event()

    async def cog_load(self):
        """Called when the cog is loaded."""
        self.session = aiohttp.ClientSession(
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "application/json",
            }
        )
        self._check_task = asyncio.create_task(self._stream_checker_loop())
        self._ready.set()
        log.info("KickAlerts cog loaded and stream checker started.")

    async def cog_unload(self):
        """Cleanup on cog unload."""
        if self._check_task:
            self._check_task.cancel()
            try:
                await self._check_task
            except asyncio.CancelledError:
                pass
        if self.session and not self.session.closed:
            await self.session.close()
        log.info("KickAlerts cog unloaded.")

    def format_help_for_context(self, ctx: commands.Context) -> str:
        """Show version in help."""
        pre = super().format_help_for_context(ctx)
        return f"{pre}\n\n**Cog Version:** {self.__version__}\n**Author:** {self.__author__}"

    # ─── API Interaction ────────────────────────────────────────────────

    async def _fetch_channel_data(self, username: str) -> Optional[Dict[str, Any]]:
        """Fetch channel data from Kick's API."""
        if not self.session or self.session.closed:
            self.session = aiohttp.ClientSession(
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Accept": "application/json",
                }
            )

        url = f"{KICK_API_BASE}/{username.lower()}"
        try:
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data
                elif resp.status == 404:
                    log.debug(f"Kick channel not found: {username}")
                    return None
                else:
                    log.warning(f"Kick API returned {resp.status} for {username}")
                    return None
        except asyncio.TimeoutError:
            log.warning(f"Timeout fetching Kick data for {username}")
            return None
        except aiohttp.ClientError as e:
            log.warning(f"HTTP error fetching Kick data for {username}: {e}")
            return None
        except Exception as e:
            log.error(f"Unexpected error fetching Kick data for {username}: {e}", exc_info=True)
            return None

    def _parse_stream_info(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Parse the API response into a clean stream info dict."""
        livestream = data.get("livestream")
        is_live = livestream is not None and livestream.get("is_live", False)

        info = {
            "is_live": is_live,
            "username": data.get("slug", data.get("user", {}).get("username", "Unknown")),
            "display_name": data.get("user", {}).get("username", data.get("slug", "Unknown")),
            "avatar_url": data.get("user", {}).get("profile_pic", None),
            "channel_url": f"{KICK_BASE_URL}/{data.get('slug', '')}",
            "followers": data.get("followersCount", 0),
            "is_verified": data.get("verified", False),
            "banner_url": data.get("banner_image", {}).get("url") if data.get("banner_image") else None,
        }

        if is_live and livestream:
            info.update({
                "stream_id": livestream.get("id"),
                "stream_title": livestream.get("session_title", "No Title"),
                "viewer_count": livestream.get("viewer_count", 0),
                "category": livestream.get("categories", [{}])[0].get("name", "Unknown")
                            if livestream.get("categories") else
                            (livestream.get("category", {}).get("name", "Unknown")
                             if livestream.get("category") else "Unknown"),
                "thumbnail_url": livestream.get("thumbnail", {}).get("url")
                                 if isinstance(livestream.get("thumbnail"), dict)
                                 else livestream.get("thumbnail"),
                "started_at": livestream.get("created_at", None),
                "language": livestream.get("language", "en"),
                "is_mature": livestream.get("is_mature", False),
                "tags": [tag.get("name", "") for tag in livestream.get("tags", [])],
            })
        else:
            info.update({
                "stream_id": None,
                "stream_title": None,
                "viewer_count": 0,
                "category": None,
                "thumbnail_url": None,
                "started_at": None,
                "language": None,
                "is_mature": False,
                "tags": [],
            })

        return info

    # ─── Embed Builder ──────────────────────────────────────────────────

    def _build_live_embed(
        self,
        info: Dict[str, Any],
        style: str = "detailed",
        show_viewers: bool = True,
        show_category: bool = True,
    ) -> discord.Embed:
        """Build a beautiful embed for a live stream announcement."""

        embed = discord.Embed(
            color=KICK_COLOR,
            timestamp=datetime.now(timezone.utc),
        )

        # Title with live indicator
        verified_badge = " ✔️" if info.get("is_verified") else ""
        embed.set_author(
            name=f"🔴 {info['display_name']}{verified_badge} is LIVE on Kick!",
            url=info["channel_url"],
            icon_url=info.get("avatar_url") or discord.Embed.Empty,
        )

        embed.title = info.get("stream_title", "No Title")
        embed.url = info["channel_url"]

        if style == "detailed":
            description_parts = []

            if show_category and info.get("category"):
                description_parts.append(f"🎮 **Category:** {info['category']}")

            if show_viewers:
                viewer_count = info.get("viewer_count", 0)
                description_parts.append(f"👁️ **Viewers:** {viewer_count:,}")

            if info.get("started_at"):
                try:
                    started = info["started_at"]
                    if isinstance(started, str):
                        # Try parsing the ISO format
                        started = started.replace("Z", "+00:00")
                        dt = datetime.fromisoformat(started)
                        description_parts.append(
                            f"🕐 **Started:** <t:{int(dt.timestamp())}:R>"
                        )
                except (ValueError, TypeError):
                    pass

            if info.get("tags"):
                tag_str = " ".join(f"`{tag}`" for tag in info["tags"][:5])
                description_parts.append(f"🏷️ **Tags:** {tag_str}")

            if info.get("is_mature"):
                description_parts.append("🔞 **Mature Content**")

            description_parts.append(
                f"\n**[Watch Stream on Kick ↗]({info['channel_url']})**"
            )

            embed.description = "\n".join(description_parts)

        else:  # minimal
            parts = []
            if show_category and info.get("category"):
                parts.append(f"Playing **{info['category']}**")
            if show_viewers:
                parts.append(f"👁️ {info.get('viewer_count', 0):,} viewers")
            parts.append(f"\n**[Watch Now ↗]({info['channel_url']})**")
            embed.description = " • ".join(parts[:2]) + (f"\n{parts[2]}" if len(parts) > 2 else "")

        # Thumbnail
        if info.get("thumbnail_url"):
            # Add cache-busting query param
            thumb = info["thumbnail_url"]
            if "?" not in thumb:
                thumb += f"?t={int(datetime.now(timezone.utc).timestamp())}"
            embed.set_image(url=thumb)
        elif info.get("avatar_url"):
            embed.set_thumbnail(url=info["avatar_url"])

        # Footer
        embed.set_footer(
            text="Kick.com • Live Stream Alert",
            icon_url="https://kick.com/favicon.ico",
        )

        return embed

    def _build_offline_embed(self, info: Dict[str, Any]) -> discord.Embed:
        """Build an embed for when a streamer goes offline."""
        embed = discord.Embed(
            color=0x808080,
            description=f"**{info['display_name']}** has gone offline.\n"
                        f"[Visit Channel ↗]({info['channel_url']})",
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_author(
            name=f"⚫ {info['display_name']} is now Offline",
            url=info["channel_url"],
            icon_url=info.get("avatar_url") or discord.Embed.Empty,
        )
        embed.set_footer(text="Kick.com • Stream Ended")
        return embed

    # ─── Stream Checker Loop ────────────────────────────────────────────

    async def _stream_checker_loop(self):
        """Background task to check streams periodically."""
        await self.bot.wait_until_ready()
        await self._ready.wait()

        while True:
            try:
                all_guilds = await self.config.all_guilds()

                for guild_id, guild_data in all_guilds.items():
                    guild = self.bot.get_guild(guild_id)
                    if not guild:
                        continue

                    streamers = guild_data.get("streamers", {})
                    if not streamers:
                        continue

                    check_interval = guild_data.get("check_interval", 60)
                    embed_style = guild_data.get("embed_style", "detailed")
                    show_viewers = guild_data.get("show_viewer_count", True)
                    show_category = guild_data.get("show_category", True)
                    auto_delete = guild_data.get("auto_delete", False)
                    global_channel_id = guild_data.get("global_channel_id")
                    global_ping_role_id = guild_data.get("global_ping_role_id")

                    for username, streamer_config in streamers.items():
                        try:
                            await self._check_single_streamer(
                                guild=guild,
                                username=username,
                                streamer_config=streamer_config,
                                embed_style=embed_style,
                                show_viewers=show_viewers,
                                show_category=show_category,
                                auto_delete=auto_delete,
                                global_channel_id=global_channel_id,
                                global_ping_role_id=global_ping_role_id,
                            )
                        except Exception as e:
                            log.error(
                                f"Error checking streamer {username} in guild {guild_id}: {e}",
                                exc_info=True,
                            )

                        # Small delay between API calls to avoid rate limits
                        await asyncio.sleep(2)

                # Get the minimum check interval across all guilds (default 60s)
                intervals = [
                    g.get("check_interval", 60) for g in all_guilds.values() if g.get("streamers")
                ]
                sleep_time = min(intervals) if intervals else 60
                sleep_time = max(sleep_time, 30)  # Never go below 30s

            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error(f"Error in stream checker loop: {e}", exc_info=True)
                sleep_time = 60

            await asyncio.sleep(sleep_time)

    async def _check_single_streamer(
        self,
        guild: discord.Guild,
        username: str,
        streamer_config: Dict[str, Any],
        embed_style: str,
        show_viewers: bool,
        show_category: bool,
        auto_delete: bool,
        global_channel_id: Optional[int],
        global_ping_role_id: Optional[int],
    ):
        """Check a single streamer and send/update announcement if needed."""
        data = await self._fetch_channel_data(username)
        if data is None:
            return

        info = self._parse_stream_info(data)

        was_live = streamer_config.get("is_live", False)
        is_live = info["is_live"]
        last_stream_id = streamer_config.get("last_stream_id")
        current_stream_id = info.get("stream_id")

        # Determine the Discord channel to post in
        channel_id = streamer_config.get("channel_id") or global_channel_id
        if not channel_id:
            return

        channel = guild.get_channel(channel_id)
        if not channel:
            return

        # Determine ping role
        ping_role_id = streamer_config.get("ping_role_id") or global_ping_role_id

        # ── Streamer went LIVE (new stream) ──
        if is_live and (not was_live or (current_stream_id and current_stream_id != last_stream_id)):
            embed = self._build_live_embed(info, embed_style, show_viewers, show_category)

            # Build ping content
            content = None
            if ping_role_id:
                role = guild.get_role(ping_role_id)
                if role:
                    content = role.mention

            custom_msg = streamer_config.get("custom_message")
            if custom_msg:
                # Support placeholders
                custom_msg = custom_msg.replace("{streamer}", info["display_name"])
                custom_msg = custom_msg.replace("{game}", info.get("category", "Unknown"))
                custom_msg = custom_msg.replace("{title}", info.get("stream_title", "No Title"))
                custom_msg = custom_msg.replace("{url}", info["channel_url"])
                custom_msg = custom_msg.replace("{viewers}", str(info.get("viewer_count", 0)))
                content = f"{content}\n{custom_msg}" if content else custom_msg

            try:
                msg = await channel.send(content=content, embed=embed)

                # Save state
                async with self.config.guild(guild).streamers() as streamers:
                    if username in streamers:
                        streamers[username]["is_live"] = True
                        streamers[username]["last_stream_id"] = current_stream_id
                        streamers[username]["last_message_id"] = msg.id

            except discord.Forbidden:
                log.warning(f"Missing permissions to send in {channel} (guild: {guild.id})")
            except discord.HTTPException as e:
                log.warning(f"Failed to send announcement for {username}: {e}")

        # ── Streamer went OFFLINE ──
        elif not is_live and was_live:
            async with self.config.guild(guild).streamers() as streamers:
                if username in streamers:
                    streamers[username]["is_live"] = False

                    last_msg_id = streamers[username].get("last_message_id")

                    # Auto-delete or update the old message
                    if last_msg_id and channel:
                        try:
                            old_msg = await channel.fetch_message(last_msg_id)

                            delete_after = streamer_config.get("delete_after_offline", auto_delete)
                            if delete_after:
                                await old_msg.delete()
                            else:
                                # Update embed to show offline
                                offline_embed = self._build_offline_embed(info)
                                await old_msg.edit(content=None, embed=offline_embed)
                        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                            pass

                    streamers[username]["last_message_id"] = None

        # ── Still live — optionally update viewer count ──
        elif is_live and was_live and embed_style == "detailed":
            # Update the existing message with new viewer count (every cycle)
            last_msg_id = streamer_config.get("last_message_id")
            if last_msg_id and channel:
                try:
                    old_msg = await channel.fetch_message(last_msg_id)
                    embed = self._build_live_embed(info, embed_style, show_viewers, show_category)
                    await old_msg.edit(embed=embed)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass

    # ─── Commands ───────────────────────────────────────────────────────

    @commands.group(name="kickalert", aliases=["ka", "kickalerts"])
    @commands.guild_only()
    @checks.admin_or_permissions(manage_guild=True)
    async def kickalert(self, ctx: commands.Context):
        """Manage Kick.com livestream alerts."""
        pass

    @kickalert.command(name="add")
    async def kickalert_add(
        self,
        ctx: commands.Context,
        username: str,
        channel: Optional[discord.TextChannel] = None,
    ):
        """Add a Kick.com streamer to monitor.

        **Arguments:**
        - `username` — The Kick.com username/slug of the streamer.
        - `channel` — (Optional) The Discord channel for announcements.
          Falls back to the global channel if not set.

        **Example:**
        `[p]kickalert add xqc #stream-alerts`
        """
        username = username.lower().strip().strip("/")

        # Validate the streamer exists
        async with ctx.typing():
            data = await self._fetch_channel_data(username)

        if data is None:
            return await ctx.send(
                f"❌ Could not find a Kick.com channel named **{username}**. "
                f"Please check the spelling and try again."
            )

        info = self._parse_stream_info(data)

        channel_id = channel.id if channel else None
        if not channel_id:
            global_ch = await self.config.guild(ctx.guild).global_channel_id()
            if not global_ch:
                return await ctx.send(
                    "❌ No channel specified and no global channel set.\n"
                    "Either provide a channel or set a global one with "
                    f"`{ctx.clean_prefix}kickalert setchannel #channel`"
                )

        async with self.config.guild(ctx.guild).streamers() as streamers:
            if username in streamers:
                return await ctx.send(
                    f"⚠️ **{info['display_name']}** is already being monitored!"
                )

            streamers[username] = {
                "channel_id": channel_id,
                "ping_role_id": None,
                "custom_message": None,
                "delete_after_offline": False,
                "last_message_id": None,
                "is_live": info["is_live"],
                "last_stream_id": info.get("stream_id"),
            }

        embed = discord.Embed(
            color=KICK_COLOR,
            title="✅ Streamer Added",
            description=(
                f"Now monitoring **[{info['display_name']}]({info['channel_url']})** on Kick.com\n\n"
                f"📺 **Channel:** {channel.mention if channel else 'Global channel'}\n"
                f"📊 **Status:** {'🔴 Currently LIVE' if info['is_live'] else '⚫ Offline'}\n"
                f"👥 **Followers:** {info.get('followers', 0):,}"
            ),
        )
        if info.get("avatar_url"):
            embed.set_thumbnail(url=info["avatar_url"])

        await ctx.send(embed=embed)

    @kickalert.command(name="remove", aliases=["delete", "rm"])
    async def kickalert_remove(self, ctx: commands.Context, username: str):
        """Remove a Kick.com streamer from monitoring.

        **Example:**
        `[p]kickalert remove xqc`
        """
        username = username.lower().strip()

        async with self.config.guild(ctx.guild).streamers() as streamers:
            if username not in streamers:
                return await ctx.send(f"❌ **{username}** is not being monitored.")
            del streamers[username]

        await ctx.send(f"✅ Removed **{username}** from Kick alerts.")

    @kickalert.command(name="list")
    async def kickalert_list(self, ctx: commands.Context):
        """List all monitored Kick.com streamers."""
        streamers = await self.config.guild(ctx.guild).streamers()

        if not streamers:
            return await ctx.send(
                "📭 No Kick.com streamers are being monitored.\n"
                f"Add one with `{ctx.clean_prefix}kickalert add <username>`"
            )

        embed = discord.Embed(
            color=KICK_COLOR,
            title="📋 Monitored Kick.com Streamers",
            timestamp=datetime.now(timezone.utc),
        )

        for username, config in streamers.items():
            channel_id = config.get("channel_id")
            global_ch = await self.config.guild(ctx.guild).global_channel_id()
            ch_id = channel_id or global_ch

            channel_mention = f"<#{ch_id}>" if ch_id else "Not set"
            status = "🔴 LIVE" if config.get("is_live") else "⚫ Offline"

            ping_role_id = config.get("ping_role_id")
            ping_text = f"<@&{ping_role_id}>" if ping_role_id else "None"

            embed.add_field(
                name=f"{status} {username}",
                value=(
                    f"📺 Channel: {channel_mention}\n"
                    f"🔔 Ping: {ping_text}\n"
                    f"🔗 [Kick Profile]({KICK_BASE_URL}/{username})"
                ),
                inline=True,
            )

        embed.set_footer(text=f"{len(streamers)} streamer(s) monitored")
        await ctx.send(embed=embed)

    @kickalert.command(name="setchannel", aliases=["channel"])
    async def kickalert_setchannel(
        self, ctx: commands.Context, channel: discord.TextChannel
    ):
        """Set the global default channel for stream alerts.

        This is used as fallback when a streamer doesn't have a specific channel set.

        **Example:**
        `[p]kickalert setchannel #live-streams`
        """
        await self.config.guild(ctx.guild).global_channel_id.set(channel.id)
        await ctx.send(f"✅ Global alert channel set to {channel.mention}")

    @kickalert.command(name="setrole", aliases=["pingrole", "role"])
    async def kickalert_setrole(
        self,
        ctx: commands.Context,
        role: discord.Role,
        username: Optional[str] = None,
    ):
        """Set a role to ping when a streamer goes live.

        If `username` is provided, sets the role for that specific streamer.
        Otherwise, sets the global ping role.

        **Examples:**
        `[p]kickalert setrole @LiveAlerts` — global ping
        `[p]kickalert setrole @LiveAlerts xqc` — only for xqc
        """
        if username:
            username = username.lower().strip()
            async with self.config.guild(ctx.guild).streamers() as streamers:
                if username not in streamers:
                    return await ctx.send(f"❌ **{username}** is not being monitored.")
                streamers[username]["ping_role_id"] = role.id
            await ctx.send(f"✅ Ping role for **{username}** set to {role.mention}")
        else:
            await self.config.guild(ctx.guild).global_ping_role_id.set(role.id)
            await ctx.send(f"✅ Global ping role set to {role.mention}")

    @kickalert.command(name="removerole")
    async def kickalert_removerole(
        self, ctx: commands.Context, username: Optional[str] = None
    ):
        """Remove the ping role for a streamer or the global ping role.

        **Examples:**
        `[p]kickalert removerole` — remove global ping
        `[p]kickalert removerole xqc` — remove ping for xqc
        """
        if username:
            username = username.lower().strip()
            async with self.config.guild(ctx.guild).streamers() as streamers:
                if username not in streamers:
                    return await ctx.send(f"❌ **{username}** is not being monitored.")
                streamers[username]["ping_role_id"] = None
            await ctx.send(f"✅ Ping role removed for **{username}**.")
        else:
            await self.config.guild(ctx.guild).global_ping_role_id.set(None)
            await ctx.send("✅ Global ping role removed.")

    @kickalert.command(name="message", aliases=["custommsg", "msg"])
    async def kickalert_message(
        self, ctx: commands.Context, username: str, *, message: str = None
    ):
        """Set a custom message for a streamer's announcement.

        **Placeholders:**
        `{streamer}` — Streamer's display name
        `{game}` — Current game/category
        `{title}` — Stream title
        `{url}` — Stream URL
        `{viewers}` — Viewer count

        **Example:**
        `[p]kickalert message xqc 🎉 {streamer} is live playing {game}! Watch: {url}`

        Use without a message to clear:
        `[p]kickalert message xqc`
        """
        username = username.lower().strip()

        async with self.config.guild(ctx.guild).streamers() as streamers:
            if username not in streamers:
                return await ctx.send(f"❌ **{username}** is not being monitored.")
            streamers[username]["custom_message"] = message

        if message:
            await ctx.send(f"✅ Custom message for **{username}** set to:\n{message}")
        else:
            await ctx.send(f"✅ Custom message for **{username}** cleared.")

    @kickalert.command(name="interval")
    async def kickalert_interval(self, ctx: commands.Context, seconds: int):
        """Set how often to check for live streams (in seconds).

        Minimum: 30 seconds. Default: 60 seconds.

        **Example:**
        `[p]kickalert interval 45`
        """
        if seconds < 30:
            return await ctx.send("❌ Minimum interval is **30 seconds** to avoid rate limits.")
        if seconds > 600:
            return await ctx.send("❌ Maximum interval is **600 seconds** (10 minutes).")

        await self.config.guild(ctx.guild).check_interval.set(seconds)
        await ctx.send(f"✅ Check interval set to **{seconds} seconds**.")

    @kickalert.command(name="style")
    async def kickalert_style(self, ctx: commands.Context, style: str):
        """Set the embed style for announcements.

        **Options:**
        - `detailed` — Full embed with category, viewers, tags, start time (default)
        - `minimal` — Compact embed with basic info

        **Example:**
        `[p]kickalert style minimal`
        """
        style = style.lower()
        if style not in ("detailed", "minimal"):
            return await ctx.send("❌ Style must be `detailed` or `minimal`.")

        await self.config.guild(ctx.guild).embed_style.set(style)
        await ctx.send(f"✅ Embed style set to **{style}**.")

    @kickalert.command(name="autodelete")
    async def kickalert_autodelete(self, ctx: commands.Context, toggle: bool):
        """Toggle auto-deleting announcements when the streamer goes offline.

        If disabled (default), the embed will be edited to show an offline status instead.

        **Example:**
        `[p]kickalert autodelete true`
        """
        await self.config.guild(ctx.guild).auto_delete.set(toggle)
        state = "enabled" if toggle else "disabled"
        await ctx.send(f"✅ Auto-delete on offline **{state}**.")

    @kickalert.command(name="test")
    async def kickalert_test(self, ctx: commands.Context, username: str):
        """Send a test announcement embed for a Kick.com streamer.

        This works even if the streamer is offline (will show mock data).

        **Example:**
        `[p]kickalert test xqc`
        """
        username = username.lower().strip()

        async with ctx.typing():
            data = await self._fetch_channel_data(username)

        if data is None:
            return await ctx.send(f"❌ Could not find Kick.com channel **{username}**.")

        info = self._parse_stream_info(data)

        # If offline, fill in mock data for preview
        if not info["is_live"]:
            info["is_live"] = True
            info["stream_title"] = info.get("stream_title") or "🔴 Test Stream — This is a Preview!"
            info["viewer_count"] = info.get("viewer_count") or 1234
            info["category"] = info.get("category") or "Just Chatting"
            info["started_at"] = datetime.now(timezone.utc).isoformat()
            info["tags"] = info.get("tags") or ["English", "Test"]

        style = await self.config.guild(ctx.guild).embed_style()
        show_viewers = await self.config.guild(ctx.guild).show_viewer_count()
        show_category = await self.config.guild(ctx.guild).show_category()

        embed = self._build_live_embed(info, style, show_viewers, show_category)

        await ctx.send("📧 **Test Announcement:**", embed=embed)

    @kickalert.command(name="check", aliases=["status"])
    async def kickalert_check(self, ctx: commands.Context, username: str):
        """Check the current live status of a Kick.com streamer.

        **Example:**
        `[p]kickalert check xqc`
        """
        username = username.lower().strip()

        async with ctx.typing():
            data = await self._fetch_channel_data(username)

        if data is None:
            return await ctx.send(f"❌ Could not find Kick.com channel **{username}**.")

        info = self._parse_stream_info(data)

        if info["is_live"]:
            embed = self._build_live_embed(info)
        else:
            embed = discord.Embed(
                color=0x808080,
                title=f"⚫ {info['display_name']} is Offline",
                url=info["channel_url"],
                description=(
                    f"**{info['display_name']}** is currently not streaming.\n\n"
                    f"👥 **Followers:** {info.get('followers', 0):,}\n"
                    f"🔗 [Visit Channel]({info['channel_url']})"
                ),
            )
            if info.get("avatar_url"):
                embed.set_thumbnail(url=info["avatar_url"])
            embed.set_footer(text="Kick.com")

        await ctx.send(embed=embed)

    @kickalert.command(name="settings")
    async def kickalert_settings(self, ctx: commands.Context):
        """View current KickAlerts configuration for this server."""
        guild_data = await self.config.guild(ctx.guild).all()

        global_ch = guild_data.get("global_channel_id")
        global_role = guild_data.get("global_ping_role_id")
        interval = guild_data.get("check_interval", 60)
        style = guild_data.get("embed_style", "detailed")
        auto_del = guild_data.get("auto_delete", False)
        show_viewers = guild_data.get("show_viewer_count", True)
        show_cat = guild_data.get("show_category", True)
        streamer_count = len(guild_data.get("streamers", {}))

        embed = discord.Embed(
            color=KICK_COLOR,
            title="⚙️ KickAlerts Settings",
            timestamp=datetime.now(timezone.utc),
        )

        embed.add_field(
            name="📺 Global Channel",
            value=f"<#{global_ch}>" if global_ch else "Not set",
            inline=True,
        )
        embed.add_field(
            name="🔔 Global Ping Role",
            value=f"<@&{global_role}>" if global_role else "None",
            inline=True,
        )
        embed.add_field(
            name="📊 Monitored Streamers",
            value=str(streamer_count),
            inline=True,
        )
        embed.add_field(
            name="⏱️ Check Interval",
            value=f"{interval}s",
            inline=True,
        )
        embed.add_field(
            name="🎨 Embed Style",
            value=style.capitalize(),
            inline=True,
        )
        embed.add_field(
            name="🗑️ Auto-Delete on Offline",
            value="Yes" if auto_del else "No",
            inline=True,
        )
        embed.add_field(
            name="👁️ Show Viewers",
            value="Yes" if show_viewers else "No",
            inline=True,
        )
        embed.add_field(
            name="🎮 Show Category",
            value="Yes" if show_cat else "No",
            inline=True,
        )

        await ctx.send(embed=embed)

    @kickalert.command(name="toggleviewers")
    async def kickalert_toggleviewers(self, ctx: commands.Context, toggle: bool):
        """Toggle showing viewer count in announcements.

        **Example:**
        `[p]kickalert toggleviewers false`
        """
        await self.config.guild(ctx.guild).show_viewer_count.set(toggle)
        state = "shown" if toggle else "hidden"
        await ctx.send(f"✅ Viewer count will be **{state}** in embeds.")

    @kickalert.command(name="togglecategory")
    async def kickalert_togglecategory(self, ctx: commands.Context, toggle: bool):
        """Toggle showing game/category in announcements.

        **Example:**
        `[p]kickalert togglecategory false`
        """
        await self.config.guild(ctx.guild).show_category.set(toggle)
        state = "shown" if toggle else "hidden"
        await ctx.send(f"✅ Category will be **{state}** in embeds.")

    @kickalert.command(name="clear")
    async def kickalert_clear(self, ctx: commands.Context, confirm: bool = False):
        """Remove ALL monitored streamers and reset configuration.

        **This cannot be undone!**

        **Example:**
        `[p]kickalert clear True`
        """
        if not confirm:
            return await ctx.send(
                "⚠️ This will remove **all** monitored streamers and reset settings.\n"
                f"Run `{ctx.clean_prefix}kickalert clear True` to confirm."
            )

        await self.config.guild(ctx.guild).clear()
        await ctx.send("✅ All KickAlerts data has been cleared for this server.")

    @kickalert.command(name="force", aliases=["forcecheck"])
    async def kickalert_force(self, ctx: commands.Context):
        """Force an immediate check of all monitored streamers.

        **Example:**
        `[p]kickalert force`
        """
        streamers = await self.config.guild(ctx.guild).streamers()
        if not streamers:
            return await ctx.send("📭 No streamers to check.")

        guild_data = await self.config.guild(ctx.guild).all()

        checked = 0
        live = 0

        async with ctx.typing():
            for username, streamer_config in streamers.items():
                try:
                    await self._check_single_streamer(
                        guild=ctx.guild,
                        username=username,
                        streamer_config=streamer_config,
                        embed_style=guild_data.get("embed_style", "detailed"),
                        show_viewers=guild_data.get("show_viewer_count", True),
                        show_category=guild_data.get("show_category", True),
                        auto_delete=guild_data.get("auto_delete", False),
                        global_channel_id=guild_data.get("global_channel_id"),
                        global_ping_role_id=guild_data.get("global_ping_role_id"),
                    )
                    checked += 1

                    # Re-read to get updated state
                    updated = await self.config.guild(ctx.guild).streamers()
                    if updated.get(username, {}).get("is_live"):
                        live += 1

                    await asyncio.sleep(1)
                except Exception as e:
                    log.error(f"Error force-checking {username}: {e}")

        await ctx.send(
            f"✅ Force-checked **{checked}** streamer(s). "
            f"**{live}** currently live."
        )