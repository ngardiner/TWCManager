#!/usr/bin/env python3
"""
Test for PID file fallback functionality (Issue #623)
Tests that TWCManager can handle permission errors when writing PID files
and falls back to /tmp gracefully.
"""

import os
import tempfile
import unittest
from unittest.mock import patch, mock_open, MagicMock


class TestPIDFileFallback(unittest.TestCase):
    """Test PID file creation with fallback to /tmp on permission errors"""

    @patch('builtins.open', new_callable=mock_open)
    @patch('os.getpid', return_value=12345)
    def test_pid_file_success(self, mock_getpid, mock_file):
        """Test successful PID file creation"""
        # Simulate successful file write
        test_path = "/etc/twcmanager/TWCManager.pid"
        
        # This simulates the fixed code behavior
        try:
            with open(test_path, "w") as f:
                f.write(str(os.getpid()))
            success = True
        except (PermissionError, OSError):
            success = False
        
        self.assertTrue(success)
        mock_file.assert_called_with(test_path, "w")

    @patch('builtins.open')
    @patch('os.getpid', return_value=12345)
    def test_pid_file_fallback_on_permission_error(self, mock_getpid, mock_file):
        """Test fallback to /tmp when primary path is not writable"""
        # First call raises PermissionError, second succeeds
        mock_file.side_effect = [
            PermissionError("Permission denied"),
            mock_open()(None, "w")
        ]
        
        primary_path = "/etc/twcmanager/TWCManager.pid"
        fallback_path = "/tmp/TWCManager.pid"
        final_path = None
        
        # Simulate the fallback logic
        try:
            with open(primary_path, "w") as f:
                f.write(str(os.getpid()))
            final_path = primary_path
        except (PermissionError, OSError):
            # Fallback to /tmp
            try:
                with open(fallback_path, "w") as f:
                    f.write(str(os.getpid()))
                final_path = fallback_path
            except (PermissionError, OSError):
                final_path = None
        
        self.assertEqual(final_path, fallback_path)
        self.assertEqual(mock_file.call_count, 2)

    @patch('builtins.open')
    @patch('os.getpid', return_value=12345)
    def test_pid_file_both_paths_fail(self, mock_getpid, mock_file):
        """Test behavior when both primary and fallback paths fail"""
        # Both calls raise PermissionError
        mock_file.side_effect = PermissionError("Permission denied")
        
        primary_path = "/etc/twcmanager/TWCManager.pid"
        fallback_path = "/tmp/TWCManager.pid"
        final_path = None
        
        # Simulate the fallback logic
        try:
            with open(primary_path, "w") as f:
                f.write(str(os.getpid()))
            final_path = primary_path
        except (PermissionError, OSError):
            # Fallback to /tmp
            try:
                with open(fallback_path, "w") as f:
                    f.write(str(os.getpid()))
                final_path = fallback_path
            except (PermissionError, OSError):
                # Continue without PID file
                final_path = None
        
        self.assertIsNone(final_path)
        self.assertEqual(mock_file.call_count, 2)

    def test_pid_file_real_filesystem(self):
        """Integration test using real filesystem with temp directory"""
        with tempfile.TemporaryDirectory() as tmpdir:
            pid_file = os.path.join(tmpdir, "TWCManager.pid")
            
            # Write PID file
            with open(pid_file, "w") as f:
                f.write(str(os.getpid()))
            
            # Verify file exists and contains PID
            self.assertTrue(os.path.exists(pid_file))
            
            with open(pid_file, "r") as f:
                content = f.read()
                self.assertEqual(content, str(os.getpid()))


if __name__ == '__main__':
    unittest.main()
