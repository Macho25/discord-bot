# pyright: reportGeneralTypeIssues=false
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

with open("/data/mc-server/config.yaml", "r") as f:
    config = yaml.safe_load(f)

load_dotenv()

DISCORD_TOKEN: str | None = os.getenv('DISCORD_TOKEN')
MIKROTIK_HOST: str | None = os.getenv('MIKROTIK_HOST')
MIKROTIK_USER: str | None = os.getenv('MIKROTIK_USER')
MIKROTIK_PASS: str | None = os.getenv('MIKROTIK_PASS')

if DISCORD_TOKEN is None:
    raise ValueError('DISCORD_TOKEN environment variable is not set')

intents: discord.Intents = discord.Intents.default()
intents.message_content = True
client: discord.Client = discord.Client(intents=intents)

SCRIPT_DIR: str = config['paths']['scripts']

class Server:
    # Tracks how many times each user started the server today: {user_id: count}
    _start_counts: dict[str, int] = {}
    MAX_STARTS_PER_DAY: int = config['session']['daily_starts']

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
        now = datetime.now()
        weekday = now.weekday()  # 0=Mon, 6=Sun
        is_weekend = weekday >= 5

        schedule = config['schedule']['weekend'] if is_weekend else config['schedule']['weekday']
        if not schedule['enabled']:
            return False

        start = datetime.strptime(schedule['start_time'], "%H:%M").time()
        shutdown = datetime.strptime(schedule['shutdown_time'], "%H:%M").time()
        current = now.time()

        # Handle overnight window e.g. 08:00 - 02:00 (weekend)
        if shutdown < start:
            return current >= start or current < shutdown
        return start <= current < shutdown

    def _should_shutdown_pc(self) -> bool:
        """Return True if current time is past the shutdown_time (PC should turn off)."""
        now = datetime.now()
        weekday = now.weekday()
        is_weekend = weekday >= 5

        schedule = config['schedule']['weekend'] if is_weekend else config['schedule']['weekday']
        shutdown = datetime.strptime(schedule['shutdown_time'], "%H:%M").time()
        current = now.time()

        # After shutdown_time means outside the allowed window
        return not self._is_within_schedule()

    # ─── Status ─────────────────────────────────────────────────────────────────

    def is_running(self) -> bool:
        """Check if the PC is reachable via ping."""
        host = config['network']['server_local_ip']
        try:
            response = ping3.ping(host, timeout=2)
            return response is not None and response is not False
        except Exception as e:
            print(f"Excection : {e}")
            return False

    def is_mc_running(self) -> bool:
        """Check if the Minecraft server is accepting connections on its port."""
        host = config['network']['server_local_ip']
        port = config['network']['minecraft_port']
        try:
            sock = socket.create_connection((host, port), timeout=3)
            sock.close()
            return True
        except OSError:
            return False

    def get_players(self) -> list[str]:
        # TODO: if no players playing, then show timer when mc server will shutdown
        response = self._rcon("list")
        if response.startswith("error:") or not response:
            return []
        # Response format: "There are X of a max of Y players online: player1, player2"
        if ":" in response:
            players_part = response.split(":")[-1].strip()
            if players_part:
                return [p.strip() for p in players_part.split(",") if p.strip()]
        return []

    def can_user_start(self, user_id: str) -> bool:
        count = self._start_counts.get(user_id, 0)
        return count < self.MAX_STARTS_PER_DAY

    def record_start(self, user_id: str) -> None:
        print(f"User {user_id}")
        self._start_counts[user_id] = self._start_counts.get(user_id, 0) + 1

    def get_status(self) -> str:
        if not self.is_running():
            return "offline"
        if not self.is_mc_running():
            return "pc online, mc starting..."
        return f"online"

    # ─── Start ──────────────────────────────────────────────────────────────────

    def send_wol(self) -> None:
        connection = routeros_api.RouterOsApiPool(
            MIKROTIK_HOST,
            username=MIKROTIK_USER,
            password=MIKROTIK_PASS,
            port=8728,
            plaintext_login=True
        )
        api = connection.get_api()
        api.get_resource('/system/script').call('run', {'number': 'send-wol'})
        connection.disconnect()

    def wol(self) -> None:
        try:
            self.send_wol()
        except Exception as e:
            print(f"❌ WoL failed: {e}")

    def start_mc(self) -> None:
        """Kill any existing tmux session and start the MC server via Docker."""
        docker_start = config['docker']['start_command']
        try:
            subprocess.run(docker_start, shell=True, timeout=30)
        except Exception as e:
            print(f"❌ Failed to start MC: {e}")

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
        self._run_script("shutdown_pc.sh")

    def stop_mc(self) -> None:
        """Stop only the MC Docker container."""
        docker_stop = config['docker']['stop_command']
        try:
            subprocess.run(docker_stop, shell=True, timeout=60)
        except Exception as e:
            print(f"❌ Failed to stop MC: {e}")

    def stop(self) -> None:
        """
        Smart stop:
        - Always stops the MC server
        - If current time is outside the allowed schedule window, also shuts down the PC
        """
        self.stop_mc()
        if self._should_shutdown_pc():
            print("🕒 Outside schedule window — shutting down PC too.")
            self.shutdown_pc()

server = Server()

@client.event
async def on_ready() -> None:
    print(f'We have logged in as {client.user}')

@client.event
async def on_message(message: discord.Message) -> None:
    username = str(message.author).split("#")[0]
    channel = str(message.channel.name)
    user_message = str(message.content)
    print(f'Message {user_message} by {username} on {channel}')

    if message.author == client.user:
        return

    if channel == "server":
        if user_message.lower() == "/start":
            if not server.can_user_start(str(message.author.id)):
                # send msg that user reached day limit
                await message.channel.send(f'❌ {username}, you have reached your daily start limit ({server.MAX_STARTS_PER_DAY}x).')
                return
            server.record_start(str(message.author.id))
            
            status: str = server.get_status()

            # NOTE: server is offline, so turn on server and then mc
            if status == "offline":
                await message.channel.send(f'🖥️ Server is currently **{status}**.')

                async def notify(msg: str):
                    await message.channel.send(msg)

                asyncio.create_task(server.start(status_callback=notify))
                return 

            # NOTE: server is online, but mc not running, so run mc
            # TODO: add protection when user call /start again
            if status == "pc online, mc starting..."
                await message.channel.send("MC server is booting up...")
                server.start_mc() 
                return

            # NOTE: everything running so just print status 

            await message.channel.send(f'🖥️ Server is currently **{status}**.')


        # WARN: NOW SHOULD NOT WORK, AND SHOULD DONT RUN
        if user_message.lower() == "/stop":
            await message.channel.send("🛑 Stopping server...")
            server.stop()
            await message.channel.send("✅ Done.")

        if user_message.lower() == "/status":
            # show if its online/offline, and uptime, and when will turn off
            status = server.get_status()
            await message.channel.send(f'🖥️ Server is currently **{status}**.')

        if user_message.lower() == "/players":
            if not server.is_mc_running():
                await message.channel.send('❌ Minecraft server is not running.')
                return
            players = server.get_players()
            if players:
                await message.channel.send(f'👥 Online players: {", ".join(players)}')
            else:
                await message.channel.send('👥 No players online or server is offline.')

    # if channel == "server":
    #     if user_message.lower() == "hello" or user_message.lower() == "hi":
    #         await message.channel.send(f'Hello {username}')
    #         return
    #     elif user_message.lower() == "bye":
    #         await message.channel.send(f'Bye {username}')
    #     elif user_message.lower() == "tell me a joke":
    #         jokes = [" Can someone please shed more\
    #         light on how my lamp got stolen?",
    #                  "Why is she called llene? She\
    #                  stands on equal legs.",
    #                  "What do you call a gazelle in a \
    #                  lions territory? Denzel."]
    #         await message.channel.send(random.choice(jokes))

client.run(DISCORD_TOKEN)
