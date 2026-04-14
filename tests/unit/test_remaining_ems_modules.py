"""
Unit tests for remaining EMS modules.

Tests cover initialization and basic functionality for:
- DSMR, DSMRreader, Efergy, EmonCMS
- IotaWatt, OpenHab, OpenWeatherMap
- P1Monitor, ScenarioEMS, SmartMe
- SolarLog, TED, URL
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
import sys

# Mock external dependencies
sys.modules['solaredge_modbus'] = MagicMock()


class TestDSMRInitialization:
    """Test DSMR module initialization."""
    
    def test_init_disabled(self):
        """Test DSMR module when disabled."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {"DSMR": {"enabled": False}}
        }
        
        with patch('TWCManager.EMS.DSMR.logger'):
            from TWCManager.EMS.DSMR import DSMR
            dsmr = DSMR(master)
            master.releaseModule.assert_called_once()
    
    def test_init_with_config(self):
        """Test DSMR module with valid config."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "DSMR": {
                    "enabled": True,
                    "port": "/dev/ttyUSB0"
                }
            }
        }
        
        with patch('TWCManager.EMS.DSMR.logger'):
            from TWCManager.EMS.DSMR import DSMR
            dsmr = DSMR(master)
            master.releaseModule.assert_not_called()


class TestEfegyInitialization:
    """Test Efergy module initialization."""
    
    def test_init_disabled(self):
        """Test Efergy module when disabled."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {"Efergy": {"enabled": False}}
        }
        
        with patch('TWCManager.EMS.Efergy.logger'):
            from TWCManager.EMS.Efergy import Efergy
            efergy = Efergy(master)
            master.releaseModule.assert_called_once()
    
    def test_init_missing_credentials(self):
        """Test Efergy module with missing credentials."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "Efergy": {
                    "enabled": True,
                    "apiKey": None
                }
            }
        }
        
        with patch('TWCManager.EMS.Efergy.logger'):
            from TWCManager.EMS.Efergy import Efergy
            efergy = Efergy(master)
            master.releaseModule.assert_called_once()


class TestEmonCMSInitialization:
    """Test EmonCMS module initialization."""
    
    def test_init_disabled(self):
        """Test EmonCMS module when disabled."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {"EmonCMS": {"enabled": False}}
        }
        
        with patch('TWCManager.EMS.EmonCMS.logger'):
            from TWCManager.EMS.EmonCMS import EmonCMS
            emoncms = EmonCMS(master)
            master.releaseModule.assert_called_once()
    
    def test_init_with_credentials(self):
        """Test EmonCMS module with valid credentials."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "EmonCMS": {
                    "enabled": True,
                    "apiKey": "test_key",
                    "url": "http://emoncms.local"
                }
            }
        }
        
        with patch('TWCManager.EMS.EmonCMS.logger'):
            from TWCManager.EMS.EmonCMS import EmonCMS
            emoncms = EmonCMS(master)
            master.releaseModule.assert_not_called()


class TestIotaWattInitialization:
    """Test IotaWatt module initialization."""
    
    def test_init_disabled(self):
        """Test IotaWatt module when disabled."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {"IotaWatt": {"enabled": False}}
        }
        
        with patch('TWCManager.EMS.IotaWatt.logger'):
            from TWCManager.EMS.IotaWatt import IotaWatt
            iotawatt = IotaWatt(master)
            master.releaseModule.assert_called_once()
    
    def test_init_with_host(self):
        """Test IotaWatt module with valid host."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "IotaWatt": {
                    "enabled": True,
                    "host": "192.168.1.100"
                }
            }
        }
        
        with patch('TWCManager.EMS.IotaWatt.logger'):
            from TWCManager.EMS.IotaWatt import IotaWatt
            iotawatt = IotaWatt(master)
            master.releaseModule.assert_not_called()


class TestOpenHabInitialization:
    """Test OpenHab module initialization."""
    
    def test_init_disabled(self):
        """Test OpenHab module when disabled."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {"OpenHab": {"enabled": False}}
        }
        
        with patch('TWCManager.EMS.OpenHab.logger'):
            from TWCManager.EMS.OpenHab import OpenHab
            openhab = OpenHab(master)
            master.releaseModule.assert_called_once()
    
    def test_init_with_url(self):
        """Test OpenHab module with valid URL."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "OpenHab": {
                    "enabled": True,
                    "url": "http://openhab.local:8080"
                }
            }
        }
        
        with patch('TWCManager.EMS.OpenHab.logger'):
            from TWCManager.EMS.OpenHab import OpenHab
            openhab = OpenHab(master)
            master.releaseModule.assert_not_called()


class TestOpenWeatherMapInitialization:
    """Test OpenWeatherMap module initialization."""
    
    def test_init_disabled(self):
        """Test OpenWeatherMap module when disabled."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {"OpenWeatherMap": {"enabled": False}}
        }
        
        with patch('TWCManager.EMS.OpenWeatherMap.logger'):
            from TWCManager.EMS.OpenWeatherMap import OpenWeatherMap
            owm = OpenWeatherMap(master)
            master.releaseModule.assert_called_once()
    
    def test_init_missing_api_key(self):
        """Test OpenWeatherMap module with missing API key."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "OpenWeatherMap": {
                    "enabled": True,
                    "apiKey": None
                }
            }
        }
        
        with patch('TWCManager.EMS.OpenWeatherMap.logger'):
            from TWCManager.EMS.OpenWeatherMap import OpenWeatherMap
            owm = OpenWeatherMap(master)
            master.releaseModule.assert_called_once()


class TestP1MonitorInitialization:
    """Test P1Monitor module initialization."""
    
    def test_init_disabled(self):
        """Test P1Monitor module when disabled."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {"P1Monitor": {"enabled": False}}
        }
        
        with patch('TWCManager.EMS.P1Monitor.logger'):
            from TWCManager.EMS.P1Monitor import P1Monitor
            p1 = P1Monitor(master)
            master.releaseModule.assert_called_once()
    
    def test_init_with_host(self):
        """Test P1Monitor module with valid host."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "P1Monitor": {
                    "enabled": True,
                    "host": "192.168.1.100"
                }
            }
        }
        
        with patch('TWCManager.EMS.P1Monitor.logger'):
            from TWCManager.EMS.P1Monitor import P1Monitor
            p1 = P1Monitor(master)
            master.releaseModule.assert_not_called()


class TestScenarioEMSInitialization:
    """Test ScenarioEMS module initialization."""
    
    def test_init_disabled(self):
        """Test ScenarioEMS module when disabled."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {"ScenarioEMS": {"enabled": False}}
        }
        
        with patch('TWCManager.EMS.ScenarioEMS.logger'):
            from TWCManager.EMS.ScenarioEMS import ScenarioEMS
            scenario = ScenarioEMS(master)
            master.releaseModule.assert_called_once()
    
    def test_init_with_scenario(self):
        """Test ScenarioEMS module with scenario file."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "ScenarioEMS": {
                    "enabled": True,
                    "scenarioFile": "tests/scenarios/green_energy/basic_surplus.json"
                }
            }
        }
        
        with patch('TWCManager.EMS.ScenarioEMS.logger'):
            from TWCManager.EMS.ScenarioEMS import ScenarioEMS
            scenario = ScenarioEMS(master)
            master.releaseModule.assert_not_called()


class TestSmartMeInitialization:
    """Test SmartMe module initialization."""
    
    def test_init_disabled(self):
        """Test SmartMe module when disabled."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {"SmartMe": {"enabled": False}}
        }
        
        with patch('TWCManager.EMS.SmartMe.logger'):
            from TWCManager.EMS.SmartMe import SmartMe
            smartme = SmartMe(master)
            master.releaseModule.assert_called_once()
    
    def test_init_missing_credentials(self):
        """Test SmartMe module with missing credentials."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "SmartMe": {
                    "enabled": True,
                    "username": None,
                    "password": None
                }
            }
        }
        
        with patch('TWCManager.EMS.SmartMe.logger'):
            from TWCManager.EMS.SmartMe import SmartMe
            smartme = SmartMe(master)
            master.releaseModule.assert_called_once()


class TestSolarLogInitialization:
    """Test SolarLog module initialization."""
    
    def test_init_disabled(self):
        """Test SolarLog module when disabled."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {"SolarLog": {"enabled": False}}
        }
        
        with patch('TWCManager.EMS.SolarLog.logger'):
            from TWCManager.EMS.SolarLog import SolarLog
            solarlog = SolarLog(master)
            master.releaseModule.assert_called_once()
    
    def test_init_with_host(self):
        """Test SolarLog module with valid host."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "SolarLog": {
                    "enabled": True,
                    "host": "192.168.1.100"
                }
            }
        }
        
        with patch('TWCManager.EMS.SolarLog.logger'):
            from TWCManager.EMS.SolarLog import SolarLog
            solarlog = SolarLog(master)
            master.releaseModule.assert_not_called()


class TestTEDInitialization:
    """Test TED module initialization."""
    
    def test_init_disabled(self):
        """Test TED module when disabled."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {"TED": {"enabled": False}}
        }
        
        with patch('TWCManager.EMS.TED.logger'):
            from TWCManager.EMS.TED import TED
            ted = TED(master)
            master.releaseModule.assert_called_once()
    
    def test_init_with_host(self):
        """Test TED module with valid host."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "TED": {
                    "enabled": True,
                    "host": "192.168.1.100"
                }
            }
        }
        
        with patch('TWCManager.EMS.TED.logger'):
            from TWCManager.EMS.TED import TED
            ted = TED(master)
            master.releaseModule.assert_not_called()


class TestURLInitialization:
    """Test URL module initialization."""
    
    def test_init_disabled(self):
        """Test URL module when disabled."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {"URL": {"enabled": False}}
        }
        
        with patch('TWCManager.EMS.URL.logger'):
            from TWCManager.EMS.URL import URL
            url = URL(master)
            master.releaseModule.assert_called_once()
    
    def test_init_missing_url(self):
        """Test URL module with missing URL."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "URL": {
                    "enabled": True,
                    "url": None
                }
            }
        }
        
        with patch('TWCManager.EMS.URL.logger'):
            from TWCManager.EMS.URL import URL
            url = URL(master)
            master.releaseModule.assert_called_once()
    
    def test_init_with_url(self):
        """Test URL module with valid URL."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {
                "URL": {
                    "enabled": True,
                    "url": "http://example.com/api/power"
                }
            }
        }
        
        with patch('TWCManager.EMS.URL.logger'):
            from TWCManager.EMS.URL import URL
            url = URL(master)
            master.releaseModule.assert_not_called()
