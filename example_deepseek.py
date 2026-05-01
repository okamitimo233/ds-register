#!/usr/bin/env python
"""
DeepSeek 注册示例脚本
演示如何使用 deepseek_register 模块进行账号注册
"""

import sys
import os

# 添加项目根目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.deepseek_register import (
    deepseek_hash_v1,
    build_prefix,
    solve_pow,
    DeepSeekChallenge,
    build_pow_header,
    solve_and_build_header,
    run_deepseek,
)
from core.register import EventEmitter


def example_hash():
    """示例：计算 DeepSeekHashV1"""
    print("\n" + "=" * 60)
    print("示例 1: 计算 DeepSeekHashV1")
    print("=" * 60)

    data = b"Hello, DeepSeek!"
    hash_result = deepseek_hash_v1(data)

    print(f"输入数据: {data}")
    print(f"Hash 结果 (hex): {hash_result.hex()}")
    print(f"Hash 长度: {len(hash_result)} 字节")


def example_pow():
    """示例：求解 PoW 挑战"""
    print("\n" + "=" * 60)
    print("示例 2: 求解 PoW 挑战（低难度）")
    print("=" * 60)

    # 构建一个简单的挑战（实际使用时从 API 获取）
    salt = "test_salt_123"
    expire_at = 9999999999
    challenge_hex = "a" * 64  # 简化的挑战（实际是 64 个十六进制字符）
    difficulty = 1000  # 低难度，仅用于演示

    print(f"Salt: {salt}")
    print(f"Expire At: {expire_at}")
    print(f"Challenge: {challenge_hex[:20]}...{challenge_hex[-20:]}")
    print(f"Difficulty: {difficulty}")
    print("\n正在求解...")

    answer = solve_pow(challenge_hex, salt, expire_at, difficulty)

    if answer is not None:
        print(f"✓ 找到解: nonce = {answer}")
    else:
        print("✗ 未找到解（这可能发生在简化挑战的情况下）")


def example_challenge_and_header():
    """示例：构建 PoW Header"""
    print("\n" + "=" * 60)
    print("示例 3: 构建验证 Header")
    print("=" * 60)

    # 模拟从 DeepSeek API 获取的挑战
    challenge_dict = {
        "algorithm": "DeepSeekHashV1",
        "challenge": "abcd1234" + "0" * 56,  # 64 字符
        "salt": "example_salt",
        "expire_at": 1735689600,
        "difficulty": 100,  # 低难度用于演示
        "signature": "example_signature",
        "target_path": "/api/v0/users/register",
    }

    challenge = DeepSeekChallenge.from_dict(challenge_dict)

    print(f"Algorithm: {challenge.algorithm}")
    print(f"Salt: {challenge.salt}")
    print(f"Difficulty: {challenge.difficulty}")
    print(f"Target Path: {challenge.target_path}")

    # 模拟找到的答案
    answer = 12345
    header = build_pow_header(challenge, answer)

    print(f"\n构建的 Header (base64):")
    print(f"{header[:50]}...")

    # 解码 Header 查看内容
    import base64
    import json

    decoded = base64.b64decode(header)
    payload = json.loads(decoded)

    print(f"\nHeader 内容:")
    print(json.dumps(payload, indent=2))


def example_registration():
    """示例：完整注册流程（仅展示接口，不实际执行）"""
    print("\n" + "=" * 60)
    print("示例 4: DeepSeek 注册流程接口")
    print("=" * 60)

    print("""
注意：完整的注册流程需要：
1. 可用的代理（避免地区限制）
2. 有效的邮箱提供商
3. DeepSeek API 可访问

以下是调用示例代码：
    """)

    code = '''
from core.deepseek_register import run_deepseek
from core.register import EventEmitter

# 创建事件发射器（用于日志输出）
emitter = EventEmitter(cli_mode=True)

# 配置 ds2api 上传（可选）
ds2api_config = {
    "enabled": True,
    "url": "http://your-ds2api-server:port",
    "admin_key": "your-admin-key",
}

# 运行 DeepSeek 注册
result = run_deepseek(
    proxy="http://127.0.0.1:7890",  # 可选代理
    emitter=emitter,
    stop_event=None,  # 可选的停止事件
    mail_provider=None,  # 使用默认邮箱提供商
    proxy_pool_config={  # 可选的代理池配置
        "enabled": False,
    },
    ds2api_config=ds2api_config,
)

if result:
    print(f"注册成功！")
    print(f"邮箱: {result['email']}")
    print(f"密码: {result['password']}")
    print(f"Token: {result['token'][:10]}...")
    print(f"已上传到 ds2api: {result['uploaded']}")
else:
    print("注册失败")
'''

    print(code)


def example_with_emitter():
    """示例：使用 EventEmitter 捕获注册事件"""
    print("\n" + "=" * 60)
    print("示例 5: 使用 EventEmitter 捕获事件")
    print("=" * 60)

    print("""
使用 EventEmitter 可以捕获注册过程中的详细事件：

from core.register import EventEmitter
import queue

# 创建事件队列
event_queue = queue.Queue()

# 创建事件发射器
emitter = EventEmitter(q=event_queue, cli_mode=False)

# 在另一个线程中运行注册
# ...

# 从队列中读取事件
while True:
    try:
        event = event_queue.get(timeout=1.0)
        print(f"[{event['level']}] {event['step']}: {event['message']}")
    except queue.Empty:
        break

事件格式：
{
    "ts": "12:34:56",
    "level": "info" | "success" | "error" | "warn",
    "step": "create_email" | "pow_challenge" | "pow_solve" | ...,
    "message": "描述信息"
}
""")


def main():
    """运行所有示例"""
    print("=" * 60)
    print("DeepSeek 注册功能示例")
    print("=" * 60)

    try:
        # 示例 1: Hash 计算
        example_hash()

        # 示例 2: PoW 求解
        example_pow()

        # 示例 3: Header 构建
        example_challenge_and_header()

        # 示例 4: 完整注册流程接口
        example_registration()

        # 示例 5: EventEmitter 使用
        example_with_emitter()

        print("\n" + "=" * 60)
        print("所有示例运行完成！")
        print("=" * 60)

    except Exception as e:
        print(f"\n错误: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
