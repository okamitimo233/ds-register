"""
Unit tests for DeepSeek PoW algorithm
"""

import struct
import unittest
import threading
import time
from core.deepseek_register import (
    deepseek_hash_v1,
    solve_pow,
    build_prefix,
    DeepSeekChallenge,
    build_pow_header,
    solve_and_build_header,
)


class TestDeepSeekHash(unittest.TestCase):
    """Test DeepSeekHashV1 algorithm"""

    def test_deepseek_hash_v1_basic(self):
        """Test basic DeepSeekHashV1 functionality"""
        # Test with empty input
        result = deepseek_hash_v1(b"")
        self.assertEqual(len(result), 32)
        self.assertIsInstance(result, bytes)

        # Test with simple input
        result = deepseek_hash_v1(b"test")
        self.assertEqual(len(result), 32)
        self.assertIsInstance(result, bytes)

        # Test with longer input
        result = deepseek_hash_v1(b"a" * 200)
        self.assertEqual(len(result), 32)
        self.assertIsInstance(result, bytes)

    def test_deepseek_hash_v1_consistency(self):
        """Test that DeepSeekHashV1 produces consistent results"""
        data = b"consistent test data"

        result1 = deepseek_hash_v1(data)
        result2 = deepseek_hash_v1(data)

        self.assertEqual(result1, result2)

    def test_deepseek_hash_v1_deterministic(self):
        """Test that same input always produces same output"""
        test_cases = [
            b"hello world",
            b"DeepSeek test",
            b"\x00\x01\x02\x03\x04",
            b"a" * 136,  # Exactly one rate block
            b"a" * 137,  # One block + 1 byte
        ]

        for data in test_cases:
            result1 = deepseek_hash_v1(data)
            result2 = deepseek_hash_v1(data)
            self.assertEqual(result1, result2, f"Inconsistent hash for input: {data[:20]}")

    def test_deepseek_hash_v1_different_inputs(self):
        """Test that different inputs produce different outputs"""
        inputs = [
            b"input1",
            b"input2",
            b"Input1",  # Case difference
            b"input10",  # Longer
        ]

        results = [deepseek_hash_v1(data) for data in inputs]

        # Check all results are unique
        for i in range(len(results)):
            for j in range(i + 1, len(results)):
                self.assertNotEqual(
                    results[i], results[j],
                    f"Hash collision between {inputs[i]} and {inputs[j]}"
                )

    def test_deepseek_hash_v1_rate_boundary(self):
        """Test DeepSeekHashV1 at rate boundaries"""
        RATE = 136

        # Test exactly at rate boundary
        data_exact = b"x" * RATE
        result_exact = deepseek_hash_v1(data_exact)
        self.assertEqual(len(result_exact), 32)

        # Test just over rate boundary
        data_over = b"x" * (RATE + 1)
        result_over = deepseek_hash_v1(data_over)
        self.assertEqual(len(result_over), 32)

        # Results should be different
        self.assertNotEqual(result_exact, result_over)

    def test_deepseek_hash_v1_multiple_blocks(self):
        """Test DeepSeekHashV1 with multiple blocks"""
        RATE = 136

        # Test with 2 full blocks
        data_2_blocks = b"x" * (RATE * 2)
        result_2_blocks = deepseek_hash_v1(data_2_blocks)
        self.assertEqual(len(result_2_blocks), 32)

        # Test with 3 full blocks + partial
        data_3_partial = b"x" * (RATE * 3 + 50)
        result_3_partial = deepseek_hash_v1(data_3_partial)
        self.assertEqual(len(result_3_partial), 32)

        # All results should be different
        self.assertNotEqual(result_2_blocks, result_3_partial)


class TestPowSolving(unittest.TestCase):
    """Test PoW solving functions"""

    def test_build_prefix(self):
        """Test prefix building"""
        salt = "test_salt"
        expire_at = 1234567890

        prefix = build_prefix(salt, expire_at)

        self.assertEqual(prefix, "test_salt_1234567890_")
        self.assertIsInstance(prefix, str)

    def test_solve_pow_simple(self):
        """Test PoW solving with low difficulty"""
        # Create a simple challenge
        # For testing, we use a very low difficulty to ensure quick solving
        challenge_hex = "a" * 64  # Simplified challenge (all 'a's)
        salt = "test_salt"
        expire_at = 9999999999
        difficulty = 100  # Very low difficulty for testing

        # This should either find a solution or return None
        # We're mainly testing that the function runs without errors
        result = solve_pow(challenge_hex, salt, expire_at, difficulty)

        # Result should be None or an integer in valid range
        self.assertTrue(result is None or (isinstance(result, int) and 0 <= result < difficulty))

    def test_solve_pow_with_stop_event(self):
        """Test that stop_event can interrupt PoW solving"""
        challenge_hex = "a" * 64
        salt = "test_salt"
        expire_at = 9999999999
        difficulty = 1000000  # High difficulty

        stop_event = threading.Event()

        # Set stop event after a short delay
        def set_stop():
            time.sleep(0.1)
            stop_event.set()

        stop_thread = threading.Thread(target=set_stop)
        stop_thread.start()

        start_time = time.time()
        result = solve_pow(challenge_hex, salt, expire_at, difficulty, stop_event=stop_event)
        elapsed = time.time() - start_time

        stop_thread.join()

        # Should return quickly due to stop_event
        self.assertLess(elapsed, 2.0, f"PoW solving took too long: {elapsed}s")
        self.assertIsNone(result, "Should return None when stopped")


class TestChallengeAndHeader(unittest.TestCase):
    """Test challenge parsing and header building"""

    def test_deepseek_challenge_from_dict(self):
        """Test DeepSeekChallenge parsing"""
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

        self.assertEqual(challenge.algorithm, "DeepSeekHashV1")
        self.assertEqual(challenge.challenge, "a" * 64)
        self.assertEqual(challenge.salt, "test_salt")
        self.assertEqual(challenge.expire_at, 1234567890)
        self.assertEqual(challenge.difficulty, 144000)
        self.assertEqual(challenge.signature, "test_signature")
        self.assertEqual(challenge.target_path, "/api/v0/users/register")

    def test_build_pow_header(self):
        """Test PoW header building"""
        import base64
        import json

        challenge = DeepSeekChallenge(
            algorithm="DeepSeekHashV1",
            challenge="a" * 64,
            salt="test_salt",
            expire_at=1234567890,
            difficulty=144000,
            signature="test_signature",
            target_path="/api/v0/users/register",
        )

        answer = 12345

        header = build_pow_header(challenge, answer)

        self.assertIsInstance(header, str)
        self.assertGreater(len(header), 0)

        # Header should be valid base64
        decoded = base64.b64decode(header)
        payload = json.loads(decoded)

        self.assertEqual(payload["algorithm"], "DeepSeekHashV1")
        self.assertEqual(payload["challenge"], "a" * 64)
        self.assertEqual(payload["salt"], "test_salt")
        self.assertEqual(payload["answer"], answer)
        self.assertEqual(payload["signature"], "test_signature")
        self.assertEqual(payload["target_path"], "/api/v0/users/register")

    def test_solve_and_build_header(self):
        """Test complete solve and build header process"""
        import base64
        import json

        challenge = DeepSeekChallenge(
            algorithm="DeepSeekHashV1",
            challenge="a" * 64,
            salt="test_salt",
            expire_at=9999999999,
            difficulty=100,  # Low difficulty for testing
            signature="test_signature",
            target_path="/api/v0/users/register",
        )

        header = solve_and_build_header(challenge)

        # Header might be None if no solution found, or a valid base64 string
        if header is not None:
            self.assertIsInstance(header, str)
            self.assertGreater(len(header), 0)

            decoded = base64.b64decode(header)
            payload = json.loads(decoded)

            self.assertIn("answer", payload)
            self.assertIsInstance(payload["answer"], int)


if __name__ == "__main__":
    unittest.main()

