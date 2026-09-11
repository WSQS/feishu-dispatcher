"""Channel 域的 agent 子树。

- :mod:`feishu_dispatcher.channel.agent.bridge` 是**唯一**依赖具体实现的地方：
  外部经它的工厂拿到 :class:`feishu_dispatcher.channel.Channel`，不接触实现类。
- :mod:`feishu_dispatcher.channel.agent.implementation` 是候选实现孵化器，只由
  bridge 引用。
"""
