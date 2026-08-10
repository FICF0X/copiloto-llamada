"""Entry point for the frozen (PyInstaller) build.

Equivalent to `python -m src.chat_app`, but as a real script at the project
root: PyInstaller's dependency analysis follows imports from wherever the
entry script lives, so putting it here (next to the `src` package, not
inside it) is what lets `from src import ...` resolve the same way it does
when run from source.
"""
from src.chat_app import main

if __name__ == "__main__":
    main()
