# pyright: reportGeneralTypeIssues=false
# TODO: set limits on starts, friend can start only /start, 
# make app working on phone, after 20 minutes turn off server 
# setup counter for it
from dotenv import load_dotenv
import os
import subprocess
import discord
from discord.ext import commands
import routeros_api
import yaml
import asyncio
import socket
import ping3
from datetime import datetime, time
import wakeonlan
from logger import log
import json

with open("/data/mc-server/config.yaml", "r") as f:
    config = yaml.safe_load(f)

load_dotenv()

DISCORD_TOKEN: str | None = os.getenv('DISCORD_TOKEN')
MIKROTIK_HOST: str | None = os.getenv('MIKROTIK_HOST')
MIKROTIK_USER: str | None = os.getenv('MIKROTIK_USER')
MIKROTIK_PASS: str | None = os.getenv('MIKROTIK_PASS')

if DISCORD_TOKEN is None:
    raise ValueError('DISCORD_TOKEN environment variable is not set')

OWNER_ID: str | None = config.get('permissions', {}).get('owner_id')
if not OWNER_ID or OWNER_ID == "YOUR_DISCORD_USER_ID_HERE":
    log.warning("Owner ID not set in config.yaml — /stop will be unavailable")

intents: discord.Intents = discord.Intents.default()
intents.message_content = True
client: discord.Client = discord.Client(intents=intents)

SCRIPT_DIR: str = config['paths']['scripts']

class Server:
    # Tracks how many times each user started the server today: {user_id: count, date: YYYY-MM-DD}
    _start_data: dict = {"date": "", "counts": {}}
    MAX_STARTS_PER_DAY: int = config['session']['daily_starts']
    _counts_file: str = "/data/mc-server/discord-bot/start_counts.json"

    def _load_counts(self) -> None:
        """Load start counts from file, reset if date has passed."""
        today = datetime.now().strftime("%Y-%m-%d")
        try:
            if os.path.exists(self._counts_file):
                with open(self._counts_file, "r") as f:
                    data = json.load(f)
                if data.get("date") == today:
                    self._start_data = data
                else:
                    # New day, reset counts
                    self._start_data = {"date": today, "counts": {}}
                    self._save_counts()
            else:
                self._start_data = {"date": today, "counts": {}}
        except Exception as e:
            log.error(f"Failed to load start counts: {e}")
            self._start_data = {"date": today, "counts": {}}

    def _save_counts(self) -> None:
        """Persist start counts to file."""
        try:
            with open(self._counts_file, "w") as f:
                json.dump(self._start_data, f)
        except Exception as e:
            log.error(f"Failed to save start counts: {e}")

    # ─── Helpers ────────────────────────────────────────────────────────────────

    def _run_script(self, script_name: str, *args) -> str:
        script_path = os.path.join(SCRIPT_DIR, script_name)
        try:
            result = subprocess.run(
                ["bash", script_path, *args],
                capture_output=True,
                text=True,
                timeout=30
            )
            return result.stdout.strip()
        except subprocess.TimeoutExpired:
            return "error: timeout"
        except Exception as e:
            return f"error: {str(e)}"

    def _rcon(self, command: str) -> str:
        """Send a command to the MC server via mcrcon CLI."""
        # WARN: not sure about that bash func mcrcon if will work like this
        try:
            result = subprocess.run(
                [
                    "mcrcon",
                    command
                ],
                capture_output=True,
                text=True,
                timeout=10
            )
            return result.stdout.strip()
        except subprocess.TimeoutExpired:
            return "error: timeout"
        except Exception as e:
            return f"error: {str(e)}"

    def _is_within_schedule(self) -> bool:
        """Return True if current time is within the allowed schedule window."""
        # TODO: this should do a script

        now = datetime.now()
        weekday = now.weekday()   # 0=Mon, 6=Sun
        is_weekend = weekday >= 5

        schedule = config['schedule']['weekend'] if is_weekend else config['schedule']['weekday']
        shutdown = datetime.strptime(schedule['shutdown_time'], "%H:%M").time()
        start = datetime.strptime(schedule['start_time'], "%H:%M").time()
        current = now.time()

        if not schedule['enabled']:
            return True

        # Handle overnight window e.g. 08:00 - 02:00 (weekend)
        if shutdown < start:
            return current >= start or current < shutdown
        return start <= current < shutdown

    def _should_shutdown_pc(self) -> bool:
        """Return True if current time is past the shutdown_time (PC should turn off)."""
        # After shutdown_time means outside the allowed window
        if self._is_within_schedule():
            return False
        else: 
            return True

    # ─── Status ─────────────────────────────────────────────────────────────────

    def is_running(self) -> bool:
        """Check if the PC is reachable via ping."""
        # TODO: this should do a script
        host = config['network']['server_local_ip']
        try:
            response = ping3.ping(host, timeout=2)
            return response is not None and response is not False
        except Exception as e:
            log.error(f"Excection : {e}")
            return False

    def is_mc_running(self) -> bool:
        """Check if the Minecraft server is accepting connections on its port."""
        try:
            if self._run_script("mc-status.sh") == "Online":
                return True
            else:
                return False

        except Exception as e:
            log.error(f"Excection : {e}")
            return False

    def get_players(self) -> list[str]:
        output = self._run_script("get-players.sh")
        if not output or output.startswith("error"):
            return []
        return [p.strip() for p in output.split("\n") if p.strip()] 

    def can_user_start(self, user_id: str) -> bool:
        self._load_counts()
        count = self._start_data["counts"].get(user_id, 0)
        return count < self.MAX_STARTS_PER_DAY

    def record_start(self, user_id: str) -> None:
        self._load_counts()
        log.info(f"User {user_id}")
        self._start_data["counts"][user_id] = self._start_data["counts"].get(user_id, 0) + 1
        self._save_counts()

    def get_status(self) -> bool:
        if self.is_running():
            return True 
        else:
            return False

    def mc_status(self) -> bool:
        if self.is_mc_running():
            return True
        else: 
            return False

    # ─── Start ──────────────────────────────────────────────────────────────────

    def send_wol(self) -> None:
        # WARN: didnt test yet
        try:
            wakeonlan.send_magic_packet('6C:62:6D:E9:8F:F5')
            log.info("✅ WoL packet sent directly")
        except Exception as e:
            log.error(f"❌ WoL failed: {e}")


    def start_mc(self) -> None:
        """Kill any existing tmux session and start the MC server via Docker."""
        docker_start = config['docker']['start_command']
        try:
            result = subprocess.run(
                docker_start,
                shell=True,
                timeout=30,
                capture_output=True,
                text=True
            )
            
            if result.returncode == 0:
                log.info("✅ MC server started successfully")
            else:
                log.error(f"❌ Command failed with exit code {result.returncode}")
                log.error(f"stdout: {result.stdout}")
                log.error(f"stderr: {result.stderr}")
                
        except subprocess.TimeoutExpired:
            log.error("❌ Start command timed out after 30s")
        except Exception as e:
            log.error(f"❌ Failed to start MC: {e}")

    def wol(self) -> None:
        try:
            self.send_wol()
        except Exception as e:
            log.error(f"❌ WoL failed: {e}")

    async def start(self, status_callback=None) -> None:
        """WoL → wait for PC to boot → start MC server."""
        self.wol()

        if status_callback:
            await status_callback("📡 WoL packet sent, waiting for PC to boot...")

        # Poll for PC to come online — give up after 2 minutes
        poll_interval = 5   # seconds between checks
        max_wait = 120       # 2 minutes
        elapsed = 0

        while elapsed < max_wait:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
            if self.is_running():
                break
        else:
            if status_callback:
                await status_callback("❌ PC did not come online within 2 minutes.")
            return

        if status_callback:
            await status_callback("✅ PC is online! Starting Minecraft server...")

        self.start_mc()

        if status_callback:
            await status_callback("🎮 Minecraft server is starting up. Join in ~1-2 min!")

    # ─── Stop ───────────────────────────────────────────────────────────────────

    def shutdown_pc(self) -> None:
        """Shut down the PC via SSH or a script. Ensures MC is stopped first."""
        if self.is_mc_running():
            self.stop_mc()
        if config['can_shutdown_pc']:
            self._run_script("shutdown.sh")
        else:
            log.info("PC cannot be shutdowned at the moment")
            return


    def stop_mc(self) -> None:
        """Stop only the MC Docker container."""
        # TODO: make a script for it
        docker_stop = config['docker']['stop_command']
        try:
            subprocess.run(docker_stop, shell=True, timeout=60)
        except Exception as e:
            log.error(f"❌ Failed to stop MC: {e}")

    def stop(self) -> None:
        """
        Smart stop:
        - Always stops the MC server
        - If current time is outside the allowed schedule window, also shuts down the PC
        """
        self.stop_mc()
        if self._should_shutdown_pc():
            log.info("🕒 Outside schedule window — shutting down PC too.")
            self.shutdown_pc()

server = Server()

@client.event
async def on_ready() -> None:
    log.info(f'We have logged in as {client.user}')

def is_owner(user_id: str) -> bool:
    """Check if the given user ID matches the configured owner."""
    return OWNER_ID is not None and str(user_id) == str(OWNER_ID)


@client.event
async def on_message(message: discord.Message) -> None:
    username = str(message.author.display_name)
    channel = str(message.channel.name)
    user_message = str(message.content)
    log.debug(f'Message {user_message} by {username} on {channel}')

    if message.author == client.user:
        return

    if channel == "server":
        if user_message.lower() == "/start":
            if not config.get('can_server_run', True):
                await message.channel.send('Server cannot be runned right now, try again later')
                return

            if not server.can_user_start(str(message.author.id)):
                # send msg that user reached day limit
                await message.channel.send(f'❌ {username}, you have reached your daily start limit ({server.MAX_STARTS_PER_DAY}x).')
                return
            server.record_start(str(message.author.id))
            
            
            if server.get_status():
                await message.channel.send('🖥️ Server is currently **online**.')

                if server.mc_status():
                    await message.channel.send('🎮 Minecraft server is **online**')
                else:
                    # NOTE: server is online, but mc not running, so run mc
                    # TODO: add protection when user call /start again
                    await message.channel.send("MC server is booting up...")
                    server.start_mc() 
                    return

            else:
                # NOTE: server is offline, so turn on server and then mc
                await message.channel.send(f'🖥️ Server is currently **offline**.')
                async def notify(msg: str):
                    await message.channel.send(msg)

                asyncio.create_task(server.start(status_callback=notify))
                return 

        if user_message.lower().startswith("/stop"):
            if not is_owner(str(message.author.id)):
                await message.channel.send("❌ Only the bot owner can stop the server.")
                return
            # Parse optional minutes argument
            parts = user_message.split()
            minutes_arg = None
            if len(parts) > 1 and parts[1].isdigit() and int(parts[1]) > 0:
                minutes_arg = parts[1]
            if minutes_arg:
                await message.channel.send(f"🛑 Stopping server in {minutes_arg} minute(s)...")
                # Run shutdown script in background
                subprocess.Popen(
                    ["bash", os.path.join(SCRIPT_DIR, "shutdown.sh"), minutes_arg],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
            else:
                await message.channel.send("🛑 Stopping server...")
                server.stop()
            await message.channel.send("✅ Done.")

        if user_message.lower() == "/status":
            # show if its online/offline, and uptime, and when will turn off
            if server.get_status():
                await message.channel.send('🖥️ Server is currently **online**.')

                if server.mc_status():
                    await message.channel.send('🎮 Minecraft server is **online**')
                    return
                else:
                    await message.channel.send('🎮 Minecraft server is **offline**')

        if user_message.lower() == "/players":
            active_players = server.get_players()
            if active_players:
                await message.channel.send(f'Players online: **{len(active_players)}**\n' + '\n'.join(f'• {p}' for p in active_players))
            else:
                await message.channel.send('No players online.')
if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
