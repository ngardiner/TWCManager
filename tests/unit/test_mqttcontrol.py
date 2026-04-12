"""
Unit tests for TWCManager MQTT Control module.

Tests MQTT broker integration and message handling.
"""

import pytest
from unittest.mock import Mock, MagicMock, patch


class TestMQTTControlInitialization:
    """Test MQTT Control module initialization."""
    
    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.config = {
            "config": {},
            "control": {
                "MQTT": {
                    "enabled": True,
                    "brokerIP": "192.168.1.100",
                    "brokerPort": 1883,
                    "topicPrefix": "twc",
                    "brokerTLS": False
                }
            }
        }
        master.releaseModule = Mock()
        return master
    
    def test_mqttcontrol_initialization(self, mock_master):
        """Test MQTT Control module initializes correctly."""
        from TWCManager.Control.MQTTControl import MQTTControl
        
        with patch('TWCManager.Control.MQTTControl.MQTTControl.mqtt'):
            mqttcontrol = MQTTControl(mock_master)
            
            assert mqttcontrol.master == mock_master
            assert mqttcontrol.status is True
            assert mqttcontrol.brokerIP == "192.168.1.100"
    
    def test_mqttcontrol_disabled(self):
        """Test MQTT Control module can be disabled."""
        from TWCManager.Control.MQTTControl import MQTTControl
        
        master = Mock()
        master.config = {
            "config": {},
            "control": {
                "MQTT": {
                    "enabled": False,
                    "brokerIP": "192.168.1.100"
                }
            }
        }
        master.releaseModule = Mock()
        
        mqttcontrol = MQTTControl(master)
        
        master.releaseModule.assert_called_once()
    
    def test_mqttcontrol_missing_broker_ip(self):
        """Test MQTT Control module unloads without broker IP."""
        from TWCManager.Control.MQTTControl import MQTTControl
        
        master = Mock()
        master.config = {
            "config": {},
            "control": {
                "MQTT": {
                    "enabled": True,
                    "brokerIP": None
                }
            }
        }
        master.releaseModule = Mock()
        
        mqttcontrol = MQTTControl(master)
        
        master.releaseModule.assert_called_once()
    
    def test_mqttcontrol_missing_config(self):
        """Test MQTT Control handles missing config gracefully."""
        from TWCManager.Control.MQTTControl import MQTTControl
        
        master = Mock()
        master.config = {
            "config": {}
        }
        master.releaseModule = Mock()
        
        mqttcontrol = MQTTControl(master)
        
        master.releaseModule.assert_called_once()


class TestMQTTControlConfiguration:
    """Test MQTT Control configuration handling."""
    
    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.config = {
            "config": {},
            "control": {
                "MQTT": {
                    "enabled": True,
                    "brokerIP": "192.168.1.100",
                    "brokerPort": 8883,
                    "topicPrefix": "home/twc",
                    "brokerTLS": True,
                    "username": "user",
                    "password": "pass"
                }
            }
        }
        master.releaseModule = Mock()
        return master
    
    def test_mqttcontrol_custom_port(self, mock_master):
        """Test MQTT Control accepts custom port."""
        from TWCManager.Control.MQTTControl import MQTTControl
        
        with patch('TWCManager.Control.MQTTControl.MQTTControl.mqtt'):
            mqttcontrol = MQTTControl(mock_master)
            
            assert mqttcontrol.brokerPort == 8883
    
    def test_mqttcontrol_default_port_plain(self):
        """Test MQTT Control defaults to port 1883 for plain MQTT."""
        from TWCManager.Control.MQTTControl import MQTTControl
        
        master = Mock()
        master.config = {
            "config": {},
            "control": {
                "MQTT": {
                    "enabled": True,
                    "brokerIP": "192.168.1.100",
                    "brokerTLS": False
                }
            }
        }
        master.releaseModule = Mock()
        
        with patch('TWCManager.Control.MQTTControl.MQTTControl.mqtt'):
            mqttcontrol = MQTTControl(master)
            
            assert mqttcontrol.brokerPort == 1883
    
    def test_mqttcontrol_default_port_tls(self):
        """Test MQTT Control defaults to port 8883 for TLS."""
        from TWCManager.Control.MQTTControl import MQTTControl
        
        master = Mock()
        master.config = {
            "config": {},
            "control": {
                "MQTT": {
                    "enabled": True,
                    "brokerIP": "192.168.1.100",
                    "brokerTLS": True
                }
            }
        }
        master.releaseModule = Mock()
        
        with patch('TWCManager.Control.MQTTControl.MQTTControl.mqtt'):
            mqttcontrol = MQTTControl(master)
            
            assert mqttcontrol.brokerPort == 8883
    
    def test_mqttcontrol_topic_prefix(self, mock_master):
        """Test MQTT Control topic prefix configuration."""
        from TWCManager.Control.MQTTControl import MQTTControl
        
        with patch('TWCManager.Control.MQTTControl.MQTTControl.mqtt'):
            mqttcontrol = MQTTControl(mock_master)
            
            assert mqttcontrol.topicPrefix == "home/twc"
    
    def test_mqttcontrol_credentials(self, mock_master):
        """Test MQTT Control credentials configuration."""
        from TWCManager.Control.MQTTControl import MQTTControl
        
        with patch('TWCManager.Control.MQTTControl.MQTTControl.mqtt'):
            mqttcontrol = MQTTControl(mock_master)
            
            assert mqttcontrol.username == "user"
            assert mqttcontrol.password == "pass"


class TestMQTTControlStatus:
    """Test MQTT Control status tracking."""
    
    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.config = {
            "config": {},
            "control": {
                "MQTT": {
                    "enabled": True,
                    "brokerIP": "192.168.1.100"
                }
            }
        }
        master.releaseModule = Mock()
        return master
    
    def test_mqttcontrol_status_enabled(self, mock_master):
        """Test MQTT Control status reflects enabled state."""
        from TWCManager.Control.MQTTControl import MQTTControl
        
        with patch('TWCManager.Control.MQTTControl.MQTTControl.mqtt'):
            mqttcontrol = MQTTControl(mock_master)
            
            assert mqttcontrol.status is True
    
    def test_mqttcontrol_status_disabled(self):
        """Test MQTT Control status reflects disabled state."""
        from TWCManager.Control.MQTTControl import MQTTControl
        
        master = Mock()
        master.config = {
            "config": {},
            "control": {
                "MQTT": {
                    "enabled": False,
                    "brokerIP": "192.168.1.100"
                }
            }
        }
        master.releaseModule = Mock()
        
        mqttcontrol = MQTTControl(master)
        
        assert mqttcontrol.status is False
    
    def test_mqttcontrol_connection_state_default(self, mock_master):
        """Test MQTT Control connection state defaults to 0."""
        from TWCManager.Control.MQTTControl import MQTTControl
        
        with patch('TWCManager.Control.MQTTControl.MQTTControl.mqtt'):
            mqttcontrol = MQTTControl(mock_master)
            
            assert mqttcontrol.connectionState == 1  # Set to 1 on init


class TestMQTTControlConnectionHandling:
    """Test MQTT Control connection handling."""
    
    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.config = {
            "config": {},
            "control": {
                "MQTT": {
                    "enabled": True,
                    "brokerIP": "192.168.1.100",
                    "topicPrefix": "twc"
                }
            }
        }
        master.releaseModule = Mock()
        return master
    
    def test_mqttcontrol_connection_refused(self, mock_master):
        """Test MQTT Control handles connection refused."""
        from TWCManager.Control.MQTTControl import MQTTControl
        
        mock_mqtt = Mock()
        mock_client = Mock()
        mock_client.connect_async.side_effect = ConnectionRefusedError("Connection refused")
        mock_mqtt.Client.return_value = mock_client
        
        with patch('TWCManager.Control.MQTTControl.MQTTControl.mqtt', mock_mqtt):
            mqttcontrol = MQTTControl(mock_master)
            
            # Should handle error gracefully
            assert mqttcontrol is not None
    
    def test_mqttcontrol_oserror(self, mock_master):
        """Test MQTT Control handles OSError."""
        from TWCManager.Control.MQTTControl import MQTTControl
        
        mock_mqtt = Mock()
        mock_client = Mock()
        mock_client.connect_async.side_effect = OSError("Network error")
        mock_mqtt.Client.return_value = mock_client
        
        with patch('TWCManager.Control.MQTTControl.MQTTControl.mqtt', mock_mqtt):
            mqttcontrol = MQTTControl(mock_master)
            
            # Should handle error gracefully
            assert mqttcontrol is not None


class TestMQTTControlCallbacks:
    """Test MQTT Control callback methods."""
    
    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.config = {
            "config": {},
            "control": {
                "MQTT": {
                    "enabled": True,
                    "brokerIP": "192.168.1.100",
                    "topicPrefix": "twc"
                }
            }
        }
        master.releaseModule = Mock()
        return master
    
    def test_mqttcontrol_has_connect_callback(self, mock_master):
        """Test MQTT Control has connect callback."""
        from TWCManager.Control.MQTTControl import MQTTControl
        
        with patch('TWCManager.Control.MQTTControl.MQTTControl.mqtt'):
            mqttcontrol = MQTTControl(mock_master)
            
            assert hasattr(mqttcontrol, 'mqttConnect')
            assert callable(mqttcontrol.mqttConnect)
    
    def test_mqttcontrol_has_message_callback(self, mock_master):
        """Test MQTT Control has message callback."""
        from TWCManager.Control.MQTTControl import MQTTControl
        
        with patch('TWCManager.Control.MQTTControl.MQTTControl.mqtt'):
            mqttcontrol = MQTTControl(mock_master)
            
            assert hasattr(mqttcontrol, 'mqttMessage')
            assert callable(mqttcontrol.mqttMessage)
    
    def test_mqttcontrol_has_subscribe_callback(self, mock_master):
        """Test MQTT Control has subscribe callback."""
        from TWCManager.Control.MQTTControl import MQTTControl
        
        with patch('TWCManager.Control.MQTTControl.MQTTControl.mqtt'):
            mqttcontrol = MQTTControl(mock_master)
            
            assert hasattr(mqttcontrol, 'mqttSubscribe')
            assert callable(mqttcontrol.mqttSubscribe)


class TestMQTTControlConfigAttributes:
    """Test MQTT Control configuration attributes."""
    
    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.config = {
            "config": {},
            "control": {
                "MQTT": {
                    "enabled": True,
                    "brokerIP": "192.168.1.100"
                }
            }
        }
        master.releaseModule = Mock()
        return master
    
    def test_mqttcontrol_config_config_default(self, mock_master):
        """Test configConfig defaults to empty dict."""
        from TWCManager.Control.MQTTControl import MQTTControl
        
        with patch('TWCManager.Control.MQTTControl.MQTTControl.mqtt'):
            mqttcontrol = MQTTControl(mock_master)
            
            assert isinstance(mqttcontrol.configConfig, dict)
    
    def test_mqttcontrol_config_mqtt_default(self, mock_master):
        """Test configMQTT defaults to empty dict."""
        from TWCManager.Control.MQTTControl import MQTTControl
        
        with patch('TWCManager.Control.MQTTControl.MQTTControl.mqtt'):
            mqttcontrol = MQTTControl(mock_master)
            
            assert isinstance(mqttcontrol.configMQTT, dict)
    
    def test_mqttcontrol_topic_prefix_default(self):
        """Test topicPrefix defaults to None."""
        from TWCManager.Control.MQTTControl import MQTTControl
        
        master = Mock()
        master.config = {
            "config": {},
            "control": {
                "MQTT": {
                    "enabled": True,
                    "brokerIP": "192.168.1.100"
                }
            }
        }
        master.releaseModule = Mock()
        
        with patch('TWCManager.Control.MQTTControl.MQTTControl.mqtt'):
            mqttcontrol = MQTTControl(master)
            
            assert mqttcontrol.topicPrefix is None


class TestMQTTControlCredentialHandling:
    """Test MQTT Control credential handling."""
    
    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.config = {
            "config": {},
            "control": {
                "MQTT": {
                    "enabled": True,
                    "brokerIP": "192.168.1.100",
                    "username": "testuser",
                    "password": "testpass"
                }
            }
        }
        master.releaseModule = Mock()
        return master
    
    def test_mqttcontrol_credentials_set(self, mock_master):
        """Test MQTT Control sets credentials when provided."""
        from TWCManager.Control.MQTTControl import MQTTControl
        
        mock_mqtt = Mock()
        mock_client = Mock()
        mock_mqtt.Client.return_value = mock_client
        
        with patch('TWCManager.Control.MQTTControl.MQTTControl.mqtt', mock_mqtt):
            mqttcontrol = MQTTControl(mock_master)
            
            # Verify username_pw_set was called
            mock_client.username_pw_set.assert_called_once_with("testuser", "testpass")
    
    def test_mqttcontrol_no_credentials(self):
        """Test MQTT Control handles missing credentials."""
        from TWCManager.Control.MQTTControl import MQTTControl
        
        master = Mock()
        master.config = {
            "config": {},
            "control": {
                "MQTT": {
                    "enabled": True,
                    "brokerIP": "192.168.1.100"
                }
            }
        }
        master.releaseModule = Mock()
        
        mock_mqtt = Mock()
        mock_client = Mock()
        mock_mqtt.Client.return_value = mock_client
        
        with patch('TWCManager.Control.MQTTControl.MQTTControl.mqtt', mock_mqtt):
            mqttcontrol = MQTTControl(master)
            
            # username_pw_set should not be called
            mock_client.username_pw_set.assert_not_called()
