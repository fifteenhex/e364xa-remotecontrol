#!/usr/bin/env python3
"""Compatibility wrapper for the old entry point.

The tool grew 661xC support and a new UI and now lives in the psuremote
package, driven by ./psu-remote. This keeps the old command line working:

    ./e364xa-remotecontrol.py --port /dev/ttyUSB0
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from psuremote.cli import main

if __name__ == "__main__":
    print("note: this is now ./psu-remote; see --help for the new options",
          file=sys.stderr)
    raise SystemExit(main())
