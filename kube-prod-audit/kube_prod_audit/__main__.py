"""Allow `python -m kube_prod_audit` to run the CLI."""
from .cli import main
import sys

if __name__ == "__main__":
    sys.exit(main())
