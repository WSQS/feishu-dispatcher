"""飞书 Channel 的兼容导入入口。"""

from .channel.feishu import FeishuBridge, FeishuConversationRef

__all__ = ["FeishuBridge", "FeishuConversationRef"]
