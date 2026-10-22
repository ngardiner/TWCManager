# Web IPC Control Module

## Introduction

The Web IPC Control module allows control of the TWCManager Tesla Wall Charger controller via an external HTTP web server, using PHP scripts. This is the web interface that was used in cdragon's TWCManager fork.

This offers decoupling of the Web Server component from TWCManager.

### PHP web interface removed

As of v1.4.0, TWCManager no longer ships the PHP web interface (`html/index.php`), and `make webinstall` no longer installs lighttpd or PHP. The PHP versions it installed are end of life and affected by PHP2Shell (CVE-2026-109618, CVE-2026-109619), unauthenticated remote code execution via php-cgi.

If you installed the legacy web interface, remove it:

```
sudo systemctl disable --now lighttpd
sudo apt-get purge 'php*' lighttpd
sudo rm /var/www/html/index.php
```

Use the built-in web interface (HTTPControl) instead. Its Debug page can send RS-485 commands. This module remains for external integrations that use the IPC queue.

### Note

In v1.2.1, we disable the use of the WebIPC interface by default. We recommend using the new HTTP Control interface.

To enable the Web IPC module, configure the following in your ```config.json``` file:

```
    "control": {
        "IPC": {
          "enabled": true
        },
```

### HTTP Control Module vs IPC Web Interface

There are two separate interfaces for managing TWCManager via web browser. These are:

   * WebIPC - The original web interface bundled with TWCManager
   * HTTPControl - The new in-built web interface
   
TWCManager no longer ships a web page for WebIPC; use HTTPControl.

### Status

| Detail          | Value          |
| --------------- | -------------- |
| **Module Name** | WebIPCControl  |
| **Module Type** | Status         |
| **Status**      | In Development |
