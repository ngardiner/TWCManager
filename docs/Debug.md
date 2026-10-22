## Introduction

The Debug Interface provides advanced control over TWCs managed by TWCManager. In normal operation, it is rare to require the use of these advanced interfaces, however if you have a use case that isn't covered by the TWCManager defaults, you may choose to make modifications using the Debug interface.

## Sending Debug Commands

The Send Debug Commands function is a powerful interface to allow direct querying of TWCs using commands that provide visibility of internal settings and values within the TWC.

### Warning

There are several known commands which do cause damage to TWCs! These are blocked by the TWCManager Debug Interface and will result in a warning being printed and the command being ignored, however there may be other commands that we are not aware of that could damage your TWC, and as a result you should limit your commands to known commands.
