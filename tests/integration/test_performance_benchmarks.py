"""
Performance and load tests for TWCManager.

Tests cover:
- API response time benchmarks
- High-frequency API calls
- Memory usage under load
- Policy selection performance
- Load-sharing calculation speed
"""

import pytest
import time
import statistics


class TestAPIResponseTime:
    """Test suite for API response time benchmarks."""
    
    def test_get_status_response_time(self, api_get):
        """Benchmark /getStatus response time."""
        times = []
        
        for i in range(10):
            start = time.time()
            response = api_get('/getStatus')
            elapsed = (time.time() - start) * 1000  # Convert to ms
            
            assert response.status_code == 200
            times.append(elapsed)
        
        avg_time = statistics.mean(times)
        max_time = max(times)
        
        # Response should be fast
        assert avg_time < 100, f"Average response time {avg_time}ms exceeds 100ms"
        assert max_time < 500, f"Max response time {max_time}ms exceeds 500ms"
    
    def test_get_config_response_time(self, api_get):
        """Benchmark /getConfig response time."""
        times = []
        
        for i in range(10):
            start = time.time()
            response = api_get('/getConfig')
            elapsed = (time.time() - start) * 1000
            
            assert response.status_code == 200
            times.append(elapsed)
        
        avg_time = statistics.mean(times)
        
        # Config retrieval should be fast
        assert avg_time < 100, f"Average response time {avg_time}ms exceeds 100ms"
    
    def test_get_policy_response_time(self, api_get):
        """Benchmark /getPolicy response time."""
        times = []
        
        for i in range(10):
            start = time.time()
            response = api_get('/getPolicy')
            elapsed = (time.time() - start) * 1000
            
            assert response.status_code == 200
            times.append(elapsed)
        
        avg_time = statistics.mean(times)
        
        # Policy retrieval should be fast
        assert avg_time < 100, f"Average response time {avg_time}ms exceeds 100ms"
    
    def test_get_slave_twcs_response_time(self, api_get):
        """Benchmark /getSlaveTWCs response time."""
        times = []
        
        for i in range(10):
            start = time.time()
            response = api_get('/getSlaveTWCs')
            elapsed = (time.time() - start) * 1000
            
            assert response.status_code == 200
            times.append(elapsed)
        
        avg_time = statistics.mean(times)
        
        # Slave enumeration should be fast
        assert avg_time < 100, f"Average response time {avg_time}ms exceeds 100ms"


class TestHighFrequencyAPICalls:
    """Test suite for high-frequency API calls."""
    
    def test_rapid_status_calls(self, api_get):
        """Test rapid /getStatus calls."""
        start = time.time()
        
        for i in range(50):
            response = api_get('/getStatus')
            assert response.status_code == 200
        
        elapsed = time.time() - start
        calls_per_second = 50 / elapsed
        
        # Should handle at least 10 calls/second
        assert calls_per_second > 10, f"Only {calls_per_second} calls/sec"
    
    def test_rapid_mixed_calls(self, api_get):
        """Test rapid mixed API calls."""
        endpoints = ['/getStatus', '/getPolicy', '/getSlaveTWCs', '/getConfig']
        start = time.time()
        
        for i in range(40):
            endpoint = endpoints[i % len(endpoints)]
            response = api_get(endpoint)
            assert response.status_code == 200
        
        elapsed = time.time() - start
        calls_per_second = 40 / elapsed
        
        # Should handle at least 5 calls/second
        assert calls_per_second > 5, f"Only {calls_per_second} calls/sec"
    
    def test_sustained_api_load(self, api_get):
        """Test sustained API load over time."""
        duration = 5  # seconds
        call_count = 0
        start = time.time()
        
        while time.time() - start < duration:
            response = api_get('/getStatus')
            assert response.status_code == 200
            call_count += 1
        
        elapsed = time.time() - start
        calls_per_second = call_count / elapsed
        
        # Should sustain at least 5 calls/second
        assert calls_per_second > 5, f"Only {calls_per_second} calls/sec"


class TestCommandResponseTime:
    """Test suite for command response time."""
    
    def test_charge_now_command_time(self, api_post):
        """Benchmark /chargeNow command time."""
        times = []
        
        for i in range(5):
            start = time.time()
            response = api_post('/chargeNow', json={
                'chargeNowRate': 24,
                'chargeNowDuration': 3600
            })
            elapsed = (time.time() - start) * 1000
            
            assert response.status_code == 200
            times.append(elapsed)
        
        avg_time = statistics.mean(times)
        
        # Command should be fast
        assert avg_time < 200, f"Average command time {avg_time}ms exceeds 200ms"
    
    def test_cancel_charge_command_time(self, api_post):
        """Benchmark /cancelChargeNow command time."""
        times = []
        
        for i in range(5):
            start = time.time()
            response = api_post('/cancelChargeNow')
            elapsed = (time.time() - start) * 1000
            
            assert response.status_code == 200
            times.append(elapsed)
        
        avg_time = statistics.mean(times)
        
        # Command should be fast
        assert avg_time < 200, f"Average command time {avg_time}ms exceeds 200ms"
    
    def test_start_stop_command_time(self, api_post):
        """Benchmark start/stop command time."""
        times = []
        
        for i in range(5):
            start = time.time()
            api_post('/sendStartCommand')
            elapsed = (time.time() - start) * 1000
            times.append(elapsed)
            
            start = time.time()
            api_post('/sendStopCommand')
            elapsed = (time.time() - start) * 1000
            times.append(elapsed)
        
        avg_time = statistics.mean(times)
        
        # Commands should be fast
        assert avg_time < 200, f"Average command time {avg_time}ms exceeds 200ms"


class TestConcurrentOperations:
    """Test suite for concurrent operations performance."""
    
    def test_concurrent_reads_performance(self, api_get):
        """Test performance of concurrent read operations."""
        start = time.time()
        
        # Simulate concurrent-like reads
        for i in range(20):
            api_get('/getStatus')
            api_get('/getPolicy')
            api_get('/getSlaveTWCs')
        
        elapsed = time.time() - start
        ops_per_second = 60 / elapsed
        
        # Should handle at least 10 ops/second
        assert ops_per_second > 10, f"Only {ops_per_second} ops/sec"
    
    def test_concurrent_read_write_performance(self, api_get, api_post):
        """Test performance of mixed read/write operations."""
        start = time.time()
        
        for i in range(10):
            api_post('/chargeNow', json={
                'chargeNowRate': 24,
                'chargeNowDuration': 3600
            })
            api_get('/getStatus')
            api_get('/getPolicy')
            api_post('/cancelChargeNow')
            api_get('/getSlaveTWCs')
        
        elapsed = time.time() - start
        ops_per_second = 50 / elapsed
        
        # Should handle at least 5 ops/second
        assert ops_per_second > 5, f"Only {ops_per_second} ops/sec"


class TestDataProcessingPerformance:
    """Test suite for data processing performance."""
    
    def test_large_response_parsing(self, api_get, assert_json_response):
        """Test parsing of large API responses."""
        times = []
        
        for i in range(5):
            start = time.time()
            response = api_get('/getStatus')
            data = assert_json_response(response, 200)
            elapsed = (time.time() - start) * 1000
            
            times.append(elapsed)
        
        avg_time = statistics.mean(times)
        
        # Parsing should be fast
        assert avg_time < 100, f"Average parsing time {avg_time}ms exceeds 100ms"
    
    def test_policy_calculation_performance(self, api_get):
        """Test policy calculation performance."""
        times = []
        
        for i in range(10):
            start = time.time()
            response = api_get('/getActivePolicyAction')
            elapsed = (time.time() - start) * 1000
            
            assert response.status_code == 200
            times.append(elapsed)
        
        avg_time = statistics.mean(times)
        
        # Policy calculation should be fast
        assert avg_time < 100, f"Average calculation time {avg_time}ms exceeds 100ms"


@pytest.mark.slow
class TestSustainedLoad:
    """Test suite for sustained load scenarios."""
    
    def test_30_second_sustained_load(self, api_get):
        """Test 30 seconds of sustained API load."""
        duration = 30
        call_count = 0
        error_count = 0
        start = time.time()
        
        while time.time() - start < duration:
            try:
                response = api_get('/getStatus')
                if response.status_code == 200:
                    call_count += 1
                else:
                    error_count += 1
            except Exception as e:
                error_count += 1
        
        elapsed = time.time() - start
        
        # Should complete without excessive errors
        assert error_count == 0, f"Had {error_count} errors during sustained load"
        assert call_count > 50, f"Only {call_count} successful calls in {duration}s"
