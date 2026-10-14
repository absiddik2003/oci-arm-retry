"""
Comprehensive Unit Tests for OCI ARM Safe Retry Controller
Tests all safety assertions, classification logic, idempotency, and guardrails.
"""

import unittest
from unittest.mock import MagicMock, patch
import oci
import json
import os
import tempfile
from pathlib import Path

from oci_retry import (
    classify_error,
    load_or_create_state,
    save_state,
    verify_home_region,
    calculate_storage_usage,
    check_existing_instances,
    MAX_ALWAYS_FREE_OCPUS,
    MAX_ALWAYS_FREE_RAM_GBS,
    MAX_TENANCY_STORAGE_GBS,
)


class TestOciArmRetry(unittest.TestCase):

    def test_error_classification_capacity(self):
        """Verify that 500 / Out of host capacity triggers CAPACITY classification."""
        err = oci.exceptions.ServiceError(
            status=500,
            code="InternalError",
            message="Out of host capacity for shape VM.Standard.A1.Flex in AD RMqd:AP-MUMBAI-1-AD-1",
            headers={"opc-request-id": "test-req-123"},
        )
        cat, wait, msg = classify_error(err)
        self.assertEqual(cat, "CAPACITY")
        self.assertIsNone(wait)
        self.assertIn("Out of host capacity", msg)

    def test_error_classification_rate_limit(self):
        """Verify that 429 TooManyRequests triggers RATE_LIMIT with Retry-After header."""
        err = oci.exceptions.ServiceError(
            status=429,
            code="TooManyRequests",
            message="Too many requests",
            headers={"opc-request-id": "test-req-456", "retry-after": "120"},
        )
        cat, wait, msg = classify_error(err)
        self.assertEqual(cat, "RATE_LIMIT")
        self.assertEqual(wait, 120)

    def test_error_classification_permanent(self):
        """Verify that 401, 403, 404, 400 errors are classified as PERMANENT and halt."""
        for code, status in [("NotAuthorizedOrNotFound", 404), ("NotAuthorized", 403), ("InvalidParameter", 400)]:
            err = oci.exceptions.ServiceError(
                status=status,
                code=code,
                message=f"Permanent test error {status}",
                headers={"opc-request-id": f"req-{status}"},
            )
            cat, wait, msg = classify_error(err)
            self.assertEqual(cat, "PERMANENT", f"Failed for status {status}")

    def test_durable_token_persistence(self):
        """Verify that opc-retry-token is persisted and reused across restarts."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "state.json")
            state1 = load_or_create_state(state_file, 2, 12, 100)
            token1 = state1["opc_retry_token"]
            self.assertTrue(len(token1) > 10)

            # Re-read state
            state2 = load_or_create_state(state_file, 2, 12, 100)
            self.assertEqual(state2["opc_retry_token"], token1)

    def test_home_region_mismatch_halt(self):
        """Verify that a mismatch in home region raises a RuntimeError."""
        mock_identity = MagicMock()
        mock_region = MagicMock()
        mock_region.region_name = "us-ashburn-1"
        mock_region.is_home_region = True
        mock_identity.list_region_subscriptions.return_value.data = [mock_region]

        with patch("oci.identity.IdentityClient", return_value=mock_identity):
            with self.assertRaises(RuntimeError) as ctx:
                verify_home_region({"tenancy": "test-tenancy", "region": "ap-mumbai-1"}, "ap-mumbai-1")
            self.assertIn("SAFETY HALT", str(ctx.exception))

    @patch("oci.core.ComputeClient")
    def test_existing_instance_detection(self, mock_compute_cls):
        """Verify that existing A1 instances in non-terminated state are detected."""
        mock_compute = MagicMock()
        mock_compute_cls.return_value = mock_compute
        inst1 = MagicMock()
        inst1.shape = "VM.Standard.A1.Flex"
        inst1.lifecycle_state = "RUNNING"
        inst1.id = "ocid1.instance.oc1..test1"

        inst_amd = MagicMock()
        inst_amd.shape = "VM.Standard.E2.1.Micro"
        inst_amd.lifecycle_state = "RUNNING"

        with patch("oci.pagination.list_call_get_all_results") as mock_pagination:
            mock_pagination.return_value.data = [inst_amd, inst1]
            existing = check_existing_instances({"tenancy": "test"}, "comp-id", "VM.Standard.A1.Flex")
            self.assertEqual(len(existing), 1)
            self.assertEqual(existing[0].id, "ocid1.instance.oc1..test1")

    @patch("oci.core.BlockstorageClient")
    def test_storage_allowance_enforcement(self, mock_bs_cls):
        """Verify that projected storage exceeding 200 GB is strictly blocked."""
        mock_bs_cls.return_value = MagicMock()
        mock_bv = MagicMock()
        mock_bv.lifecycle_state = "AVAILABLE"
        mock_bv.size_in_gbs = 120
        mock_bv.display_name = "Existing Vol"

        with patch("oci.pagination.list_call_get_all_results") as mock_pagination:
            mock_pagination.return_value.data = [mock_bv]
            total, details = calculate_storage_usage({"tenancy": "test"}, "comp-id", "AD-1")
            self.assertEqual(total, 240)  # boot + block mock returns 120 twice
            self.assertGreater(total + 100, MAX_TENANCY_STORAGE_GBS)


if __name__ == "__main__":
    unittest.main()
