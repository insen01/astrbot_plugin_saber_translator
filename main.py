import os
import io
import re
import zipfile
import base64
import shutil
import tempfile
import httpx
import traceback
from PIL import Image

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star
from astrbot.api import logger
import astrbot.api.message_components as Comp

def get_image_url_or_path(component):
    for attr in ['url', 'path', 'file', 'image_url', 'image_path']:
        if hasattr(component, attr):
            val = getattr(component, attr)
            if val:
                return val
    return None

def get_file_url_or_path(component):
    for attr in ['url', 'path', 'file', 'file_url', 'file_path']:
        if hasattr(component, attr):
            val = getattr(component, attr)
            if val:
                return val
    return None

async def get_image_base64(url_or_path: str) -> str:
    """获取图片的 Base64 编码"""
    if not url_or_path:
        return None
    # 本地文件
    if os.path.exists(url_or_path):
        with open(url_or_path, "rb") as f:
            return base64.b64encode(f.read()).decode('utf-8')
    # 远程 URL
    if url_or_path.startswith("http"):
        async with httpx.AsyncClient() as client:
            resp = await client.get(url_or_path, timeout=30)
            if resp.status_code == 200:
                return base64.b64encode(resp.content).decode('utf-8')
    return None

async def download_file_bytes(url_or_path: str) -> bytes:
    """下载或读取文件为二进制数据"""
    if not url_or_path:
        return None
    if os.path.exists(url_or_path):
        with open(url_or_path, "rb") as f:
            return f.read()
    if url_or_path.startswith("http"):
        async with httpx.AsyncClient() as client:
            resp = await client.get(url_or_path, timeout=60)
            if resp.status_code == 200:
                return resp.content
    return None

class SaberTranslatorPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        # 用于保存各个 Session 最新发送的图片或 zip 压缩包，做交互容错缓存
        # 格式为: {session_id: ("image"|"zip", component)}
        self.last_assets = {}

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message_cache(self, event: AstrMessageEvent):
        """缓存每条消息中的多媒体文件，方便后续翻译指令无缝调取。"""
        session_id = event.message_obj.session_id
        for component in event.message_obj.message:
            # 检查图片
            img_val = get_image_url_or_path(component)
            if img_val:
                self.last_assets[session_id] = ("image", component)
                logger.debug(f"Saber-Translator: 缓存了会话 {session_id} 的图片: {img_val}")
                return
            # 检查 zip
            file_val = get_file_url_or_path(component)
            if file_val and isinstance(file_val, str) and file_val.lower().endswith(".zip"):
                self.last_assets[session_id] = ("zip", component)
                logger.debug(f"Saber-Translator: 缓存了会话 {session_id} 的 zip 压缩包: {file_val}")
                return

    @filter.command("trans_comic_test")
    async def translate_comic_test(self, event: AstrMessageEvent):
        """测试远程 Astrbot 到本地 Saber-Translator 翻译引擎的连接及健康状态。"""
        config = self.context.get_config() or {}
        saber_url = config.get("saber_base_url", "http://127.0.0.1:5000").rstrip("/")
        yield event.plain_result(f"📡 正在测试与本地翻译引擎的连接，目标地址: {saber_url} ...")
        diag = await self._test_connection(saber_url)
        yield event.plain_result(diag["msg"])

    async def _test_connection(self, saber_url: str) -> dict:
        """测试远程服务器到本地 Saber-Translator 的连接状态，并返回诊断信息"""
        try:
            async with httpx.AsyncClient(timeout=6.0) as client:
                resp = await client.get(f"{saber_url}/api/get_settings")
                if resp.status_code == 200:
                    settings_data = resp.json().get("settings", {})
                    trans_settings = settings_data.get("translation", {})
                    provider = trans_settings.get("provider", "未设置")
                    return {
                        "success": True, 
                        "msg": f"✅ 连接成功！本地 Saber-Translator 响应正常。\n- 本地已配置大模型服务商: {provider}"
                    }
                else:
                    return {
                        "success": False, 
                        "msg": f"❌ 连接异常！Saber-Translator 返回了错误的状态码: HTTP {resp.status_code}\n(这通常表示接口存在但有内部异常，请检查本地项目日志。)"
                    }
        except httpx.ConnectTimeout:
            return {
                "success": False, 
                "msg": f"❌ 连接超时！远程服务器无法连接至本地 IP: {saber_url}\n\n💡 排查建议：\n1. 确认本地电脑和远程服务器的 Tailscale 均处于在线状态。\n2. 确认您填写的 Tailscale IP 端口是否正确。\n3. 极有可能：Windows Defender 防火墙拦截了 5000 端口。请在 Windows 控制面板 -> 系统和安全 -> Windows Defender 防火墙 -> 高级设置中，新建【入站规则】，放行端口 5000 的 TCP 连接。"
            }
        except (httpx.ConnectError, httpx.NetworkError) as ce:
            return {
                "success": False,
                "msg": f"❌ 连接被拒绝或网络不可达！目标地址: {saber_url}\n\n💡 排查建议：\n1. 确认本地电脑上 Saber-Translator 项目已经正常启动并正在运行。\n2. 检查本地命令行窗口中 Flask 服务是否仍然存活在 5000 端口。\n3. 详细报错: {str(ce)}"
            }
        except Exception as e:
            return {
                "success": False,
                "msg": f"❌ 网络请求发生未知错误: {str(e)}"
            }

    @filter.command("trans_comic")
    async def translate_comic_cmd(self, event: AstrMessageEvent):
        """通过指令触发漫画翻译"""
        async for result in self._do_translate_comic(event):
            yield result

    @filter.llm_tool(name="translate_comic")
    async def translate_comic_tool(self, event: AstrMessageEvent) -> MessageEventResult:
        """翻译用户提供的漫画图片或打包的多图漫画压缩包 (ZIP)。"""
        async for result in self._do_translate_comic(event):
            yield result

    async def _do_translate_comic(self, event: AstrMessageEvent):
        session_id = event.message_obj.session_id
        target_component = None
        asset_type = None

        # 1. 优先从当前消息体中查找图片或 zip 压缩包
        for component in event.message_obj.message:
            img_val = get_image_url_or_path(component)
            if img_val:
                target_component = component
                asset_type = "image"
                break
            file_val = get_file_url_or_path(component)
            if file_val and isinstance(file_val, str) and file_val.lower().endswith(".zip"):
                target_component = component
                asset_type = "zip"
                break

        # 2. 如果当前消息未携带，则从会话缓存中调取最新上传的资源
        if not target_component and session_id in self.last_assets:
            asset_type, target_component = self.last_assets[session_id]
            logger.info(f"Saber-Translator: 当前指令未直接携带图片，自动调取会话缓存资源类型: {asset_type}")

        if not target_component:
            yield event.plain_result("❌ 未检测到需要翻译的图片或 zip 压缩包。\n💡 请先发送图片/压缩包，或者在发送该指令时一并附带图片。")
            return

        # 3. 读取插件配置
        config = self.context.get_config() or {}
        saber_url = config.get("saber_base_url", "http://127.0.0.1:5000").rstrip("/")
        
        # 3.5 前置连接可用性验证
        diag = await self._test_connection(saber_url)
        if not diag["success"]:
            yield event.plain_result(f"⚠️ 无法启动漫画翻译管线，因为到本地翻译项目的网络连接失败！\n\n{diag['msg']}")
            return

        # 4. 执行翻译流水线
        if asset_type == "image":
            yield event.plain_result("⏳ 正在获取图片并启动漫画翻译管线，请稍候...")
            try:
                img_val = get_image_url_or_path(target_component)
                img_b64 = await get_image_base64(img_val)
                if not img_b64:
                    raise ValueError("无法提取图片数据")

                # 执行原子链翻译
                final_b64 = await self._run_translation_pipeline(saber_url, img_b64, config)
                
                # 回传结果
                # 在 data 目录下创建临时文件
                temp_dir = os.path.join("data", "astrbot_saber_temp")
                os.makedirs(temp_dir, exist_ok=True)
                output_path = os.path.join(temp_dir, f"translated_{session_id}.png")
                
                with open(output_path, "wb") as f:
                    f.write(base64.b64decode(final_b64))

                yield event.chain_result([
                    Comp.Plain("✅ 漫画翻译完成！效果如下：\n"),
                    Comp.Image.fromFileSystem(output_path)
                ])

            except Exception as e:
                logger.error(traceback.format_exc())
                yield event.plain_result(f"❌ 漫画图片翻译失败，错误信息:\n{str(e)}")

        elif asset_type == "zip":
            yield event.plain_result("⏳ 正在下载并解压漫画包，执行批量翻译流程，请耐心等待...")
            
            # 创建专用临时工作区
            work_dir = os.path.join("data", "astrbot_saber_temp", f"zip_{session_id}")
            if os.path.exists(work_dir):
                shutil.rmtree(work_dir)
            os.makedirs(work_dir, exist_ok=True)

            try:
                file_val = get_file_url_or_path(target_component)
                file_bytes = await download_file_bytes(file_val)
                if not file_bytes:
                    raise ValueError("无法获取压缩包数据")

                zip_path = os.path.join(work_dir, "input.zip")
                with open(zip_path, "wb") as f:
                    f.write(file_bytes)

                # 解压缩
                extract_dir = os.path.join(work_dir, "extracted")
                os.makedirs(extract_dir, exist_ok=True)
                with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                    zip_ref.extractall(extract_dir)

                # 搜集图片列表
                supported_exts = ('.png', '.jpg', '.jpeg', '.webp')
                img_files = []
                for root, _, files in os.walk(extract_dir):
                    for file in files:
                        if file.lower().endswith(supported_exts):
                            img_files.append(os.path.join(root, file))

                # 按文件名排序，保证阅读连贯
                img_files.sort()
                total_imgs = len(img_files)

                if total_imgs == 0:
                    raise ValueError("压缩包内未找到任何支持的图片文件")

                yield event.plain_result(f"📦 成功识别到 {total_imgs} 张图片。开始批量处理...")

                output_images_dir = os.path.join(work_dir, "translated")
                os.makedirs(output_images_dir, exist_ok=True)

                success_count = 0
                for idx, img_path in enumerate(img_files):
                    try:
                        # 读取单张图 base64
                        with open(img_path, "rb") as f:
                            img_b64 = base64.b64encode(f.read()).decode('utf-8')
                        
                        # 翻译
                        final_b64 = await self._run_translation_pipeline(saber_url, img_b64, config)
                        
                        # 保存翻译后图片，保持原有文件名
                        base_name = os.path.basename(img_path)
                        save_path = os.path.join(output_images_dir, base_name)
                        with open(save_path, "wb") as f:
                            f.write(base64.b64decode(final_b64))
                        success_count += 1
                        
                        if (idx + 1) % 5 == 0 or (idx + 1) == total_imgs:
                            logger.info(f"Saber-Translator: 批量处理中 ({idx + 1}/{total_imgs})...")
                    except Exception as single_err:
                        logger.error(f"翻译单张图片时出错 {img_path}: {single_err}")

                if success_count == 0:
                    raise RuntimeError("所有漫画图片均处理失败，请检查翻译引擎配置。")

                # 打包压缩包
                out_zip_path = os.path.join("data", "astrbot_saber_temp", f"translated_comic_{session_id}.zip")
                if os.path.exists(out_zip_path):
                    os.remove(out_zip_path)

                with zipfile.ZipFile(out_zip_path, 'w', zipfile.ZIP_DEFLATED) as zip_out:
                    for root, _, files in os.walk(output_images_dir):
                        for file in files:
                            full_p = os.path.join(root, file)
                            rel_p = os.path.relpath(full_p, output_images_dir)
                            zip_out.write(full_p, rel_p)

                yield event.chain_result([
                    Comp.Plain(f"🎉 漫画翻译完成！\n成功率: {success_count}/{total_imgs}\n已为您重新打包为 ZIP 文件下载。"),
                    Comp.File(file=out_zip_path, name=f"translated_comic_{session_id}.zip")
                ])

            except Exception as e:
                logger.error(traceback.format_exc())
                yield event.plain_result(f"❌ 漫画压缩包翻译失败，错误信息:\n{str(e)}")
            finally:
                # 清理工作目录
                try:
                    if os.path.exists(work_dir):
                        shutil.rmtree(work_dir)
                except Exception:
                    pass

    async def _run_translation_pipeline(self, saber_url: str, img_b64: str, config: dict) -> str:
        """串联 Saber-Translator 5个原子步骤 API"""
        async with httpx.AsyncClient(timeout=120.0) as client:
            headers = {"Content-Type": "application/json"}

            # 0. 尝试拉取本地的配置以进行参数缺省回退
            local_provider, local_model, local_key, local_base_url = None, None, None, None
            try:
                res_settings = await client.get(f"{saber_url}/api/get_settings")
                if res_settings.status_code == 200:
                    settings_data = res_settings.json().get("settings", {})
                    trans_settings = settings_data.get("translation", {})
                    local_provider = trans_settings.get("provider")
                    local_model = trans_settings.get("modelName")
                    local_key = trans_settings.get("apiKey")
                    local_base_url = trans_settings.get("customBaseUrl")
                    logger.info(f"Saber-Translator: 已自动同步本地翻译引擎配置，服务商: {local_provider}")
            except Exception as e:
                logger.warning(f"Saber-Translator: 自动获取本地设置失败 (将使用插件手动配置): {e}")

            final_provider = config.get("model_provider") or local_provider or "siliconflow"
            final_model = config.get("model_name") or local_model or "deepseek-ai/DeepSeek-V3"
            final_key = config.get("api_key") or local_key or ""
            final_base_url = config.get("custom_base_url") or local_base_url or ""

            # Step 1: Detect 气泡检测
            detect_payload = {
                "image": img_b64,
                "detector_type": config.get("detector_type", "default")
            }
            res_detect = await client.post(f"{saber_url}/api/parallel/detect", json=detect_payload, headers=headers)
            if res_detect.status_code != 200:
                raise RuntimeError(f"气泡检测接口异常: HTTP {res_detect.status_code}")
            detect_data = res_detect.json()
            if not detect_data.get("success"):
                raise RuntimeError(f"气泡检测失败: {detect_data.get('error', '未知错误')}")

            bubble_coords = detect_data.get("bubble_coords", [])
            if not bubble_coords:
                logger.info("Saber-Translator: 未在该漫画中识别出任何文本气泡，跳过翻译直接渲染。")
                return img_b64

            # Step 2: OCR 识别
            ocr_payload = {
                "image": img_b64,
                "bubble_coords": bubble_coords,
                "source_language": config.get("source_language", "japanese")
            }
            res_ocr = await client.post(f"{saber_url}/api/parallel/ocr", json=ocr_payload, headers=headers)
            if res_ocr.status_code != 200:
                raise RuntimeError(f"OCR识别接口异常: HTTP {res_ocr.status_code}")
            ocr_data = res_ocr.json()
            if not ocr_data.get("success"):
                raise RuntimeError(f"OCR识别失败: {ocr_data.get('error', '未知错误')}")

            original_texts = ocr_data.get("original_texts", [])

            # Step 3: Translate 翻译
            translate_payload = {
                "original_texts": original_texts,
                "source_language": config.get("source_language", "japanese"),
                "target_language": config.get("target_language", "zh"),
                "model_provider": final_provider,
                "model_name": final_model,
                "api_key": final_key,
                "custom_base_url": final_base_url
            }
            res_trans = await client.post(f"{saber_url}/api/parallel/translate", json=translate_payload, headers=headers)
            
            # 当返回 HTTP 错误或逻辑错误时，尽可能解出原始的报错给用户，例如大模型 401
            if res_trans.status_code != 200:
                trans_err_msg = f"HTTP {res_trans.status_code}"
                try:
                    trans_err_msg = res_trans.json().get("error", trans_err_msg)
                except Exception:
                    pass
                raise RuntimeError(f"翻译引擎异常: {trans_err_msg}")
            
            trans_data = res_trans.json()
            if not trans_data.get("success"):
                raise RuntimeError(f"大模型翻译失败: {trans_data.get('error', '未知错误')}")

            translated_texts = trans_data.get("translated_texts", [])

            # Step 4: Inpaint 图像背景去字修复
            inpaint_payload = {
                "image": img_b64,
                "bubble_coords": bubble_coords
            }
            res_inpaint = await client.post(f"{saber_url}/api/parallel/inpaint", json=inpaint_payload, headers=headers)
            if res_inpaint.status_code != 200:
                raise RuntimeError(f"背景去字修复接口异常: HTTP {res_inpaint.status_code}")
            inpaint_data = res_inpaint.json()
            if not inpaint_data.get("success"):
                raise RuntimeError(f"背景去字修复失败: {inpaint_data.get('error', '未知错误')}")

            clean_image = inpaint_data.get("clean_image")

            # Step 5: Render 渲染译文到图片
            bubble_states = []
            for i in range(len(bubble_coords)):
                translated_text = translated_texts[i] if i < len(translated_texts) else ""
                original_text = original_texts[i] if i < len(original_texts) else ""
                
                coords = bubble_coords[i]
                bubble_polygons = detect_data.get("bubble_polygons", [])
                polygon = bubble_polygons[i] if i < len(bubble_polygons) else []
                
                textlines_per_bubble = ocr_data.get("textlines_per_bubble", [])
                textlines = textlines_per_bubble[i] if i < len(textlines_per_bubble) else []

                bubble_states.append({
                    "originalText": original_text,
                    "translatedText": translated_text,
                    "coords": coords,
                    "polygon": polygon,
                    "textlines": textlines
                })

            render_payload = {
                "clean_image": clean_image,
                "bubble_states": bubble_states,
                "autoFontSize": True
            }
            res_render = await client.post(f"{saber_url}/api/parallel/render", json=render_payload, headers=headers)
            if res_render.status_code != 200:
                raise RuntimeError(f"译文文字渲染接口异常: HTTP {res_render.status_code}")
            render_data = res_render.json()
            if not render_data.get("success"):
                raise RuntimeError(f"译文文字渲染失败: {render_data.get('error', '未知错误')}")

            return render_data.get("final_image")
