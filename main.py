from re import S
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger
from astrbot.api.message_components import *
import asyncio
import pyzipper 
from pathlib import Path
import aiofiles

@register("Video2Zip", "Joker42S", "视频转ZIP", "1.0.0")
class Video2Zip(Star):
    def __init__(self, context: Context, config : dict):
        super().__init__(context)
        self.config = config

    async def initialize(self):
        """插件初始化方法"""
        try:
            self.plugin_name = "Video2Zip"
            
            self.base_dir = StarTools.get_data_dir(self.plugin_name)
            # 创建临时目录用于存储生成的压缩文件
            self.temp_dir = self.base_dir / "temp"
            #清理临时文件
            await self._cleanup_temp_files()
            if not self.temp_dir.exists():
                self.temp_dir.mkdir(parents=True, exist_ok=True)
            self.debug_mode = self.config.get("debug_mode")
            if self.debug_mode:
                self.whitelist = self.config.get("debug_whitelist")
                self.target_qq = self.config.get("debug_target_qq")
                logger.info('调试模式开启，将使用调试配置')
            else:
                self.whitelist = self.config.get("whitelist")
                self.target_qq = self.config.get("target_qq")
            self.whilelist_enable = self.config.get("whilelist_enable", True)
            self.zip_password = self.config.get("zip_password")

            # 检查配置是否完整
            if not self.whitelist or not self.target_qq or self.whitelist.count == 0:
                logger.warning("TG2QQ插件配置不完整，请检查source_tg和target_qq配置")

            logger.info("插件初始化完成")
        except Exception as e:
            logger.error(f"TG2QQ插件初始化失败: {e}")

    async def _cleanup_temp_files(self):
        """清理临时文件"""
        try:
            if self.temp_dir and self.temp_dir.exists():
                import shutil
                shutil.rmtree(self.temp_dir)
                logger.info("清理临时文件完成")
        except Exception as e:
            logger.error(f"清理临时文件失败: {e}")


    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def watch_qq_message(self, event: AstrMessageEvent):
        """监听QQ私信消息"""
        if event.get_platform_name() != "aiocqhttp":
            return
        try:
            if not self.whitelist or not self.target_qq:
                return
            # 检查消息是否来自指定的白名单
            sender_qq = event.get_sender_id()
            sender_name = event.get_sender_name()
            if self.whilelist_enable and not str(sender_qq) in self.whitelist:
                logger.warning(f"收到来自未知QQ号 {sender_qq} , {sender_name}的消息")
                yield event.plain_result(f"你没有私信权限, 请联系管理员添加白名单")
                return
            
            file_name = ""
            src_file_path = None
            is_supported_type = True
            if self.debug_mode:
                logger.info(f"Debug EVENT: {event.get_messages()}")
            for msg in event.get_messages():
                if self.debug_mode:
                    logger.info(f"Debug MSG: {msg}")
                if isinstance(msg, Video):
                    src_file_path = msg.path
                    file_name = msg.file
                    if not Path(src_file_path).is_file():
                        yield event.plain_result("开始下载视频")
                        await self._download_qq_video(event, file_name)
                    break
                elif isinstance(msg, File):
                    yield event.plain_result("开始下载文件")
                    _src_file_path = await msg.get_file()
                    _file_name = Path(_src_file_path).name
                    file_name = msg.name
                    if file_name == '':
                        file_name = msg.url.split('name=')[-1]
                    #更改文件名为url中的原始文件名
                    src_file_path = _src_file_path.replace(_file_name, file_name)
                    safe_rename(_src_file_path, src_file_path)
                    break
                elif isinstance(msg, Image):
                    async for result in self.forward_image(event, msg, sender_qq, sender_name):
                        yield result
            if not src_file_path:
                yield event.plain_result("已转发消息中的图片到群聊，未发现视频/文件。")
                return
            
            yield event.plain_result("开始打包视频/文件...")
            # 构建转发消息
            forward_message1 = MessageChain()
            forward_message2 = MessageChain()
            forward_message3 = MessageChain()
            file_name = f"{file_name}.zip"
            zip_file_path = self.temp_dir / file_name
            await self._compress_file(zip_file_path, src_file_path, self.zip_password)
            forward_message1.chain.append(File(file = str(zip_file_path), name = file_name))
            forward_message2.chain.append(Plain("压缩包来自："))
            forward_message2.chain.append(At(qq = sender_qq))
            preview_image_paths = await self._capture_video_preview(src_file_path)
            if len(preview_image_paths) > 0:
                logger.info(f"视频预览图路径：{preview_image_paths}")
                forward_message3.chain.append(Plain("视频预览："))
                for preview_image_path in preview_image_paths:
                    forward_message3.chain.append(Image.fromFileSystem(preview_image_path))
            # 发送压缩后的文件到目标QQ群
            yield event.plain_result(f"正在打包发送，请稍等")
            try:
                await self.context.send_message(f"aiocqhttp:GroupMessage:{self.target_qq}",forward_message1)
                await self.context.send_message(f"aiocqhttp:GroupMessage:{self.target_qq}",forward_message2)
                await self.context.send_message(f"aiocqhttp:GroupMessage:{self.target_qq}",forward_message3)
            except Exception as e:
                logger.error(f"发送失败: {e}")
                yield event.plain_result(f"发送失败: {e}，请联系管理员")
                return
            logger.info(f"成功压缩视频/文件并发送到QQ群： {self.target_qq}")
            yield event.plain_result(f"发送成功")
        except Exception as e:
            logger.error(f"处理消息失败: {e}")
            yield event.plain_result(f"发送失败: {e}，请联系管理员")

    async def forward_image(self, event, image_msg: Image, sender_qq, sender_name):
        """转发图片"""
        image_path = await image_msg.convert_to_file_path()
        image_name = Path(image_path).name
        new_image_path = str(self.temp_dir / image_name)
        async with aiofiles.open(image_path, 'rb') as f:
            img_data = await f.read()
        img_data = await _image_obfus(img_data)
        async with aiofiles.open(new_image_path, 'wb') as f:
            await f.write(img_data)
        node = Node(
            uin = sender_qq,
            name = sender_name,
            content = [Image.fromFileSystem(new_image_path)]
        )
        forward_message = MessageChain()
        forward_message.chain.append(node)
        try:
            await self.context.send_message(f"aiocqhttp:GroupMessage:{self.target_qq}",forward_message)
        except Exception as e:
            logger.error(f"发送失败: {e}")
            yield event.plain_result(f"图片太涩了，机器人也发不出来喵！({image_name})")

    async def _compress_file(self, zip_file_path, src_file_path: str, zip_password: str = ""):
        """压缩文件"""
        if self.debug_mode:
            logger.info(f"Debug ZIP source: {src_file_path}")
        def sync_zip_creation():
            with pyzipper.AESZipFile(zip_file_path, 'w', compression=pyzipper.ZIP_STORED, encryption=pyzipper.WZ_AES) as zipf:
                if zip_password != "":
                    zipf.setpassword(zip_password.encode('utf-8'))
                zipf.write(src_file_path, arcname = os.path.basename(src_file_path))
        await asyncio.to_thread(sync_zip_creation)

    async def _download_qq_video(self, event, file_name):
        client = event.bot  # 得到 client
        payloads = {
            "file": file_name
        }
        ret = await client.api.call_action('get_file', **payloads)  # 调用 协议端  API
        return ret

    async def _capture_video_preview(self, video_path):
        file_name = Path(video_path).name
        if file_name.split('.')[-1] not in ['mp4', 'mov', 'avi', 'mkv', 'flv', 'wmv', 'webm']:
            return []
        preview_image_paths = []
        """异步调用ffprobe获取视频时长（秒）"""
        cmd = [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "json",
            video_path
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        info = json.loads(stdout)
        duration = float(info['format']['duration'])

        for i, time_sec in enumerate([duration * 0.2, duration * 0.6]):
            output_path = self.temp_dir / f"{file_name}_preview_{i}.jpg"
            """截取视频预览图"""
            cmd = [
                "ffmpeg",
                "-ss", str(time_sec),
                "-i", video_path,
                "-vframes", "1",
                "-q:v", "2",  # 质量参数，数值越小质量越好
                output_path,
                "-y"  # 覆盖输出文件
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                logger.info(f"截取预览图出错： {time_sec}s: {stderr.decode()}")
            else:
                preview_image_paths.append(str(output_path))
        return preview_image_paths

    async def terminate(self):
        """插件退出方法"""
        logger.info("开始清理临时文件")
        await self._cleanup_temp_files()

def safe_rename(src, dst, max_attempts=99):
    if not os.path.exists(dst):
        os.rename(src, dst)
        print(f"重命名成功：{dst}")
        return dst

    base, ext = os.path.splitext(dst)

    for i in range(1, max_attempts + 1):
        new_dst = f"{base}_{i}{ext}"
        if not os.path.exists(new_dst):
            os.rename(src, new_dst)
            print(f"目标文件存在，重命名为 {new_dst}")
            return new_dst

    raise FileExistsError(f"无法找到未占用的文件名（尝试了 {max_attempts} 个序号）")

async def _image_obfus(img_data):
    """破坏图片哈希"""
    from PIL import Image as ImageP
    from io import BytesIO
    import random

    try:
        with BytesIO(img_data) as input_buffer:
            with ImageP.open(input_buffer) as img:
                if img.mode != "RGB":
                    img = img.convert("RGB")

                width, height = img.size
                pixels = img.load()

                points = []
                for _ in range(3):
                    while True:
                        x = random.randint(0, width - 1)
                        y = random.randint(0, height - 1)
                        if (x, y) not in points:
                            points.append((x, y))
                            break

                for x, y in points:
                    r, g, b = pixels[x, y]

                    r_change = random.choice([-1, 1])
                    g_change = random.choice([-1, 1])
                    b_change = random.choice([-1, 1])

                    new_r = max(0, min(255, r + r_change))
                    new_g = max(0, min(255, g + g_change))
                    new_b = max(0, min(255, b + b_change))

                    pixels[x, y] = (new_r, new_g, new_b)

                with BytesIO() as output:
                    img.save(output, format="PNG")
                    return output.getvalue()

    except Exception as e:
        logger.warning(f"破坏图片哈希时发生错误: {str(e)}")
        return img_data
    