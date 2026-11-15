# MacroPad Mixer - boot.py
# Runs only on power-up / hard reset. Enables the second USB serial port ("data")
# that the host daemon uses; the first one stays the normal REPL console.
import usb_cdc

usb_cdc.enable(console=True, data=True)
