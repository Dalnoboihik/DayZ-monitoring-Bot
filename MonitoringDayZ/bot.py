import discord
from discord.ext import commands, tasks
import asyncio
import json
import logging
import socket
import struct
from typing import Dict, Optional

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Загрузка конфигурации
def load_config():
    try:
        with open('config.json', 'r', encoding='utf-8') as f:
            content = f.read()
            if not content.strip():
                logger.error("config.json пуст")
                return []
            return json.loads(content)
    except FileNotFoundError:
        logger.error("Файл config.json не найден")
        return []
    except json.JSONDecodeError as e:
        logger.error(f"Ошибка в формате JSON: {e}")
        return []

class DayZServer:
    def __init__(self, name: str, token: str, ip: str, port: int, offline: str, template: str):
        self.name = name
        self.token = token
        self.ip = ip
        self.port = port
        self.offline = offline
        self.template = template
        self.last_status = None

class DayZMonitorBot:
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = False
        self.bot = commands.Bot(command_prefix='!', intents=intents, help_command=None)
        self.servers: Dict[str, DayZServer] = {}
        self.current_status_index = 0
        self.setup_events()
        
    def setup_events(self):
        @self.bot.event
        async def on_ready():
            logger.info(f'Бот {self.bot.user} запущен и готов к работе!')
            logger.info(f"Отслеживается серверов: {len(self.servers)}")
            self.update_status.start()
        
        @self.bot.event
        async def on_message(message):
            pass
    
    def parse_dayz_response(self, data: bytes) -> dict:
        """
        Полный парсинг ответа DayZ сервера
        """
        info = {
            'players': 0,
            'max_players': 0,
            'time': '--:--',
            'queue': 0
        }
        
        if len(data) < 5:
            return info
            
        offset = 5  # Пропускаем заголовок 0xFF 0xFF 0xFF 0xFF
        
        try:
            # Протокол (1 байт)
            if offset >= len(data): return info
            offset += 1
            
            # Название сервера (string)
            if offset >= len(data): return info
            name_end = data.find(b'\x00', offset)
            if name_end == -1: return info
            info['server_name'] = data[offset:name_end].decode('utf-8', errors='ignore')
            offset = name_end + 1
            
            # Карта (string)
            if offset >= len(data): return info
            map_end = data.find(b'\x00', offset)
            if map_end == -1: return info
            info['map'] = data[offset:map_end].decode('utf-8', errors='ignore')
            offset = map_end + 1
            
            # Папка игры (string)
            if offset >= len(data): return info
            folder_end = data.find(b'\x00', offset)
            if folder_end == -1: return info
            offset = folder_end + 1
            
            # Название игры (string)
            if offset >= len(data): return info
            game_end = data.find(b'\x00', offset)
            if game_end == -1: return info
            offset = game_end + 1
            
            # ID игры (short)
            if offset + 2 > len(data): return info
            offset += 2
            
            # Игроки (byte)
            if offset >= len(data): return info
            info['players'] = data[offset]
            offset += 1
            
            # Макс игроков (byte)
            if offset >= len(data): return info
            info['max_players'] = data[offset]
            offset += 1
            
            # Боты (byte)
            if offset >= len(data): return info
            offset += 1
            
            # Тип сервера (byte as char)
            if offset >= len(data): return info
            offset += 1
            
            # ОС (byte as char)
            if offset >= len(data): return info
            offset += 1
            
            # Видимость (byte)
            if offset >= len(data): return info
            offset += 1
            
            # VAC (byte)
            if offset >= len(data): return info
            offset += 1
            
            # Версия (string)
            if offset < len(data):
                version_end = data.find(b'\x00', offset)
                if version_end != -1:
                    info['version'] = data[offset:version_end].decode('utf-8', errors='ignore')
                    offset = version_end + 1
            
            # **Парсим время и очередь**
            
            # Сначала ищем время (4 байта - минуты с полуночи)
            if offset + 4 <= len(data):
                time_raw = struct.unpack('<I', data[offset:offset+4])[0]
                # Проверяем, что это разумное время (0-1440 минут)
                if 0 <= time_raw <= 1440:
                    hours = time_raw // 60
                    minutes = time_raw % 60
                    info['time'] = f"{hours:02d}:{minutes:02d}"
                    logger.info(f"Найдено время: {info['time']} ({time_raw} минут)")
                    offset += 4
                    
                    # После времени обычно идет очередь (2 байта)
                    if offset + 2 <= len(data):
                        queue_raw = struct.unpack('<H', data[offset:offset+2])[0]
                        # Проверяем что очередь не огромная (максимум 999)
                        if queue_raw < 1000:
                            info['queue'] = queue_raw
                            logger.info(f"Найдена очередь: {info['queue']}")
                            offset += 2
                        else:
                            # Если число слишком большое - это не очередь
                            logger.warning(f"Странное значение очереди: {queue_raw}, пропускаем")
            
            # Если не нашли время в 4 байтах, пробуем 2 байта (HHMM)
            if info['time'] == '--:--' and offset + 2 <= len(data):
                time_raw = struct.unpack('<H', data[offset:offset+2])[0]
                hours = time_raw // 100
                minutes = time_raw % 100
                if 0 <= hours < 24 and 0 <= minutes < 60:
                    info['time'] = f"{hours:02d}:{minutes:02d}"
                    logger.info(f"Найдено время (HHMM): {info['time']}")
                    offset += 2
                    
                    # После времени очередь
                    if offset + 2 <= len(data):
                        queue_raw = struct.unpack('<H', data[offset:offset+2])[0]
                        if queue_raw < 1000:
                            info['queue'] = queue_raw
                            logger.info(f"Найдена очередь: {info['queue']}")
            
            # Если время не найдено, ищем в тексте
            if info['time'] == '--:--' and offset < len(data):
                remaining = data[offset:].decode('utf-8', errors='ignore')
                import re
                time_pattern = r'([0-9]{1,2}:[0-9]{2})'
                match = re.search(time_pattern, remaining)
                if match:
                    info['time'] = match.group(1)
                    logger.info(f"Найдено время в тексте: {info['time']}")
                    
                    # После текста с временем может быть очередь
                    time_pos = remaining.find(match.group(1))
                    after_time = remaining[time_pos + len(match.group(1)):]
                    queue_match = re.search(r'(\d+)', after_time)
                    if queue_match:
                        queue_val = int(queue_match.group(1))
                        if queue_val < 1000:
                            info['queue'] = queue_val
            
        except Exception as e:
            logger.error(f"Ошибка парсинга: {e}")
        
        # Финальная проверка очереди (не даем огромных чисел)
        if info['queue'] > 999:
            logger.warning(f"Сброс некорректной очереди: {info['queue']} -> 0")
            info['queue'] = 0
            
        return info
    
    async def query_server(self, server: DayZServer) -> Optional[dict]:
        """Запрос информации о сервере через UDP"""
        try:
            loop = asyncio.get_event_loop()
            data = await loop.run_in_executor(None, self._udp_query, server.ip, server.port)
            
            if data and len(data) > 5:
                info = self.parse_dayz_response(data)
                
                logger.info(f"Сервер {server.name}: {info.get('players', 0)}/{info.get('max_players', 0)} игроков, "
                          f"время: {info.get('time', '--:--')}, очередь: {info.get('queue', 0)}")
                return info
                
        except Exception as e:
            logger.error(f"Ошибка при запросе к серверу {server.name}: {e}")
            return None
    
    def _udp_query(self, ip: str, port: int) -> bytes:
        """Синхронный UDP запрос"""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(5)
        
        try:
            request = b'\xFF\xFF\xFF\xFFTSource Engine Query\x00'
            sock.sendto(request, (ip, port))
            data, addr = sock.recvfrom(4096)
            return data
        except socket.timeout:
            logger.error(f"Таймаут при запросе к {ip}:{port}")
            raise
        finally:
            sock.close()
    
    async def update_server_status(self, server: DayZServer) -> str:
        """Обновление статуса сервера"""
        try:
            info = await self.query_server(server)
            
            if info:
                status = server.template.format(
                    players=info.get('players', 0),
                    slots=info.get('max_players', 0),
                    time=info.get('time', '--:--'),
                    queue=info.get('queue', 0)
                )
                server.last_status = status
                return status
            else:
                server.last_status = server.offline
                return server.offline
                
        except Exception as e:
            logger.error(f"Ошибка при обновлении статуса {server.name}: {e}")
            server.last_status = server.offline
            return server.offline
    
    @tasks.loop(seconds=10)
    async def update_status(self):
        """Обновление статуса бота в Discord"""
        if not self.servers:
            await self.bot.change_presence(
                activity=discord.Game(name="🔴 Нет серверов для мониторинга"),
                status=discord.Status.idle
            )
            return
            
        try:
            servers_list = list(self.servers.values())
            server = servers_list[self.current_status_index]
            self.current_status_index = (self.current_status_index + 1) % len(servers_list)
            
            status_text = await self.update_server_status(server)
            
            await self.bot.change_presence(
                activity=discord.Game(name=status_text),
                status=discord.Status.online
            )
            
            logger.info(f"Статус обновлен: {status_text}")
            
        except Exception as e:
            logger.error(f"Ошибка при обновлении статуса: {e}")
    
    def load_servers(self):
        """Загрузка серверов из конфигурации"""
        config = load_config()
        if not config:
            logger.warning("Конфигурация пуста или не загружена")
            return
            
        for server_config in config:
            try:
                if isinstance(server_config, dict):
                    server = DayZServer(**server_config)
                    self.servers[server.name] = server
                    logger.info(f"Загружен сервер: {server.name}")
                else:
                    logger.error(f"Неверный формат конфигурации: {server_config}")
            except Exception as e:
                logger.error(f"Ошибка при загрузке сервера: {e}")
    
    def run(self, token: str):
        """Запуск бота"""
        self.load_servers()
        if not self.servers:
            logger.warning("Нет загруженных серверов!")
        self.bot.run(token)

if __name__ == "__main__":
    BOT_TOKEN = "-----------token-------------"
    
    monitor_bot = DayZMonitorBot()
    monitor_bot.run(BOT_TOKEN)