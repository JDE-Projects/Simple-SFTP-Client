import os
import sys

# Make the project root (one level up from tests/) importable so
# `import simple_sftp_client` finds the module during test collection.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
