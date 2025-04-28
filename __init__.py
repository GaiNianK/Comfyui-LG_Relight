import torch
import numpy as np
from PIL import Image
import io
import base64
from server import PromptServer
import threading
from aiohttp import web
import json
import torch.nn.functional as F
image_cache = {}
event_dict = {}

class LG_Relight_Basic:

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
                "normals": ("IMAGE",),
                "x": ("FLOAT", { 
                    "default": 0.5, 
                    "min": 0.0, 
                    "max": 1.0, 
                    "step": 0.001 
                }),
                "y": ("FLOAT", { 
                    "default": 0.5, 
                    "min": 0.0, 
                    "max": 1.0, 
                    "step": 0.001 
                }),
                "z": ("FLOAT", { 
                    "default": 1.0, 
                    "min": -1.0, 
                    "max": 1.0, 
                    "step": 0.001 
                }),
                "brightness": ("FLOAT", { 
                    "default": 1.0, 
                    "min": 0.0, 
                    "max": 3.0, 
                    "step": 0.001 
                }),
                "shadow_range": ("FLOAT", { 
                    "default": 1.0, 
                    "min": 0.0, 
                    "max": 2.0, 
                    "step": 0.001 
                }),
                "shadow_strength": ("FLOAT", { 
                    "default": 1.0, 
                    "min": 0.0, 
                    "max": 2.0, 
                    "step": 0.001 
                }),
                "highlight_range": ("FLOAT", { 
                    "default": 1.0, 
                    "min": 0.0, 
                    "max": 2.0, 
                    "step": 0.001 
                }),
                "highlight_strength": ("FLOAT", { 
                    "default": 1.0, 
                    "min": 0.0, 
                    "max": 2.0, 
                    "step": 0.001 
                }),
                "highlight_color": ("STRING", {"default": "#FFFFFF"}),
                "shadow_color": ("STRING", {"default": "#000000"}),
            }
        }

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "relight"
    CATEGORY = "🎈LAOGOU"

    def relight(self, image, normals, x, y, z, brightness, 
              shadow_range, shadow_strength, highlight_range, highlight_strength,
              highlight_color, shadow_color):
        
        # 将十六进制颜色转换为RGB值
        def hex_to_rgb(hex_color):
            hex_color = hex_color.lstrip('#')
            return [int(hex_color[i:i+2], 16)/255.0 for i in (0, 2, 4)]
        
        highlight_color = hex_to_rgb(highlight_color)
        shadow_color = hex_to_rgb(shadow_color)
        
        norm = normals.detach().clone() * 2 - 1
        norm = F.interpolate(norm.movedim(-1,1), 
                        size=(image.shape[1], image.shape[2]), 
                        mode='bilinear').movedim(1,-1)

        # 将百分比坐标转换为光照方向
        # 确保光源位置与坐标一致
        light_x = -((x * 2) - 1)  # 0->-1, 1->1
        # 注意这里y要反转，因为图像坐标系y轴向下，而光照坐标系y轴向上
        light_y = -((y * 2) - 1)  # 0->1, 1->-1
        
        light = torch.tensor([light_x, light_y, z], device=image.device)
        light = F.normalize(light, dim=0)

        diffuse = norm[:,:,:,0] * light[0] + norm[:,:,:,1] * light[1] + norm[:,:,:,2] * light[2]
        diffuse = (diffuse + 1.0) * 0.5
        
        shadow_offset = shadow_strength - 1.0
        highlight_offset = highlight_strength - 1.0

        shadow_threshold = 1.0 - shadow_range
        highlight_threshold = 1.0 - highlight_range
        
        shadow_mask = torch.clamp((diffuse - shadow_threshold) / max(shadow_range, 1e-6), 0, 1)
        highlight_mask = torch.clamp((diffuse - highlight_threshold) / max(highlight_range, 1e-6), 0, 1)

        light_intensity = torch.ones_like(diffuse)

        if shadow_strength != 1.0:
            light_intensity = light_intensity * (
                shadow_mask + 
                (1.0 - shadow_mask) * (2.0 - shadow_strength)
            )

        if highlight_strength != 1.0:
            light_intensity = light_intensity + highlight_mask * highlight_offset

        color_effect = torch.ones_like(image[:,:,:,:3])
        if highlight_color != [1.0, 1.0, 1.0] or shadow_color != [0.0, 0.0, 0.0]:
            highlight_color = torch.tensor(highlight_color, device=image.device)
            shadow_color = torch.tensor(shadow_color, device=image.device)
            color_effect = (
                shadow_mask.unsqueeze(-1) * highlight_color +
                (1.0 - shadow_mask).unsqueeze(-1) * shadow_color
            )

        brightness_factor = brightness if brightness != 1.0 else 1.0

        relit = image.detach().clone()
        light_intensity = light_intensity.unsqueeze(-1).repeat(1,1,1,3)
        
        relit[:,:,:,:3] = torch.clip(
            relit[:,:,:,:3] * light_intensity * brightness_factor * color_effect,
            0, 1
        )
        
        return (relit,)
    


class LG_Relight:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
                "normals": ("IMAGE",),
            },
            "hidden": {
                "unique_id": "UNIQUE_ID",
            }
        }

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "relight"
    CATEGORY = "🎈LAOGOU"
    OUTPUT_NODE = True

    def encode_image_to_base64(self, image, is_mask=False):
        image = (image * 255).clip(0, 255).astype(np.uint8)
        
        if is_mask:
            if len(image.shape) == 3:
                image = image[0]
            image = np.stack([image] * 3, axis=-1)
        else:
            if len(image.shape) == 4:
                image = image[0]
        
        image = Image.fromarray(image)
        buffered = io.BytesIO()
        image.save(buffered, format="PNG")
        return base64.b64encode(buffered.getvalue()).decode()

    def relight(self, image, normals, unique_id):
        try:
            event = threading.Event()
            event_dict[unique_id] = event
            image_b64 = self.encode_image_to_base64(image.cpu().numpy())
            normals_b64 = self.encode_image_to_base64(normals.cpu().numpy())
            send_data = {
                "node_id": unique_id,
                "image": f"data:image/png;base64,{image_b64}",
                "normals": f"data:image/png;base64,{normals_b64}"
            }
            PromptServer.instance.send_sync("lg_relight_init", send_data)
            event.wait()
            del event_dict[unique_id]
            
            if unique_id in image_cache:
                img_data = base64.b64decode(image_cache[unique_id].split(",")[1])
                img = Image.open(io.BytesIO(img_data))
                img_np = np.array(img).astype(np.float32) / 255.0
                
                if len(img_np.shape) == 2:
                    img_np = np.stack([img_np] * 3, axis=-1)
                elif len(img_np.shape) == 3 and img_np.shape[-1] == 4:
                    img_np = img_np[..., :3]
                
                result = torch.from_numpy(img_np).unsqueeze(0)
                
                del image_cache[unique_id]
                return (result,)
            else:
                return (image,)
            
        except Exception as e:
            print(f"[ERROR] relight处理发生错误: {str(e)}")
            import traceback
            print(traceback.format_exc())
            return (image,)
        finally:
            print(f"[DEBUG] 清理资源: node_id={unique_id}")
            if unique_id in event_dict:
                del event_dict[unique_id]
            if unique_id in image_cache:
                del image_cache[unique_id]

@PromptServer.instance.routes.post("/lg_relight/update_image")
async def update_image_v3(request):
    try:
        data = await request.json()
        node_id = data.get("node_id")
        image_data = data.get("image")
        
        if node_id and image_data:
            image_cache[node_id] = image_data
            if node_id in event_dict:
                event_dict[node_id].set()
            return web.Response(text=json.dumps({"status": "success"}))
        else:
            return web.Response(status=400, text=json.dumps({"error": "无效数据"}))
    except Exception as e:
        return web.Response(status=500, text=json.dumps({"error": str(e)}))

@PromptServer.instance.routes.post("/lg_relight/cancel")
async def cancel_v3(request):
    try:
        data = await request.json()
        node_id = data.get("node_id")
        
        if node_id:
            if node_id in event_dict:
                event_dict[node_id].set()
            return web.Response(text=json.dumps({"status": "success"}))
        else:
            return web.Response(status=400, text=json.dumps({"error": "无效节点ID"}))
    except Exception as e:
        return web.Response(status=500, text=json.dumps({"error": str(e)}))



WEB_DIRECTORY = "web"

NODE_CLASS_MAPPINGS = {
    "LG_Relight_Basic": LG_Relight_Basic,
    "LG_Relight": LG_Relight
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "LG_Relight_Basic": "LG Relight Basic",
    "LG_Relight": "LG Relight"
}
