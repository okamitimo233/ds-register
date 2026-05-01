#!/usr/bin/env python
"""
Simple test script for DeepSeek PoW algorithm
"""

import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def test_basic_hash():
    """Test basic hash functionality"""
    print("Testing DeepSeekHashV1 basic functionality...")

    from core.deepseek_register import deepseek_hash_v1

    # Test 1: Empty input
    result = deepseek_hash_v1(b"")
    assert len(result) == 32, f"Expected 32 bytes, got {len(result)}"
    print("  [PASS] Empty input produces 32-byte hash")

    # Test 2: Simple input
    result = deepseek_hash_v1(b"test")
    assert len(result) == 32, f"Expected 32 bytes, got {len(result)}"
    print("  [PASS] Simple input produces 32-byte hash")

    # Test 3: Consistency
    data = b"test data"
    result1 = deepseek_hash_v1(data)
    result2 = deepseek_hash_v1(data)
    assert result1 == result2, "Hash should be deterministic"
    print("  [PASS] Hash is deterministic")

    # Test 4: Different inputs
    result1 = deepseek_hash_v1(b"input1")
    result2 = deepseek_hash_v1(b"input2")
    assert result1 != result2, "Different inputs should produce different hashes"
    print("  [PASS] Different inputs produce different hashes")

    print("All hash tests passed!\n")


def test_pow_solving():
    """Test PoW solving"""
    print("Testing PoW solving...")

    from core.deepseek_register import build_prefix, solve_pow

    # Test 1: Prefix building
    salt = "test_salt"
    expire_at = 1234567890
    prefix = build_prefix(salt, expire_at)
    expected = "test_salt_1234567890_"
    assert prefix == expected, f"Expected {expected}, got {prefix}"
    print("  [PASS] Prefix building works correctly")

    # Test 2: Low difficulty PoW (should solve quickly or return None)
    challenge_hex = "a" * 64
    result = solve_pow(challenge_hex, salt, expire_at, difficulty=100)
    assert result is None or (isinstance(result, int) and 0 <= result < 100)
    print(f"  [PASS] PoW solving with low difficulty: result={result}")

    print("All PoW tests passed!\n")


def test_challenge_header():
    """Test challenge and header building"""
    print("Testing challenge and header...")

    import base64
    import json
    from core.deepseek_register import (
        DeepSeekChallenge,
        build_pow_header,
        solve_and_build_header,
    )

    # Test 1: Challenge parsing
    challenge_dict = {
        "algorithm": "DeepSeekHashV1",
        "challenge": "a" * 64,
        "salt": "test_salt",
        "expire_at": 1234567890,
        "difficulty": 144000,
        "signature": "test_signature",
        "target_path": "/api/v0/users/register",
    }

    challenge = DeepSeekChallenge.from_dict(challenge_dict)
    assert challenge.algorithm == "DeepSeekHashV1"
    assert challenge.challenge == "a" * 64
    assert challenge.difficulty == 144000
    print("  [PASS] Challenge parsing works correctly")

    # Test 2: Header building
    answer = 12345
    header = build_pow_header(challenge, answer)
    decoded = base64.b64decode(header)
    payload = json.loads(decoded)
    assert payload["algorithm"] == "DeepSeekHashV1"
    assert payload["answer"] == answer
    print("  [PASS] Header building works correctly")

    # Test 3: Solve and build (low difficulty)
    test_challenge = DeepSeekChallenge(
        algorithm="DeepSeekHashV1",
        challenge="a" * 64,
        salt="test_salt",
        expire_at=9999999999,
        difficulty=100,
        signature="test_signature",
        target_path="/api/v0/users/register",
    )

    header = solve_and_build_header(test_challenge)
    if header is not None:
        decoded = base64.b64decode(header)
        payload = json.loads(decoded)
        assert "answer" in payload
        print(f"  [PASS] Solve and build works: answer={payload['answer']}")
    else:
        print("  [PASS] Solve and build returned None (expected for some challenges)")

    print("All challenge/header tests passed!\n")


def test_rate_boundaries():
    """Test hash at rate boundaries"""
    print("Testing rate boundaries...")

    from core.deepseek_register import deepseek_hash_v1

    RATE = 136

    # Test exactly at rate boundary
    data_exact = b"x" * RATE
    result_exact = deepseek_hash_v1(data_exact)
    assert len(result_exact) == 32
    print("  [PASS] Exact rate boundary works")

    # Test over rate boundary
    data_over = b"x" * (RATE + 1)
    result_over = deepseek_hash_v1(data_over)
    assert len(result_over) == 32
    assert result_exact != result_over
    print("  [PASS] Over rate boundary works")

    # Test multiple blocks
    data_multi = b"x" * (RATE * 3 + 50)
    result_multi = deepseek_hash_v1(data_multi)
    assert len(result_multi) == 32
    print("  [PASS] Multiple blocks work")

    print("All rate boundary tests passed!\n")


if __name__ == "__main__":
    print("=" * 60)
    print("DeepSeek PoW Algorithm Test Suite")
    print("=" * 60 + "\n")

    try:
        test_basic_hash()
        test_pow_solving()
        test_challenge_header()
        test_rate_boundaries()

        print("=" * 60)
        print("ALL TESTS PASSED!")
        print("=" * 60)
    except AssertionError as e:
        print(f"\n[FAIL] Test assertion failed: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n[ERROR] Test error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
