"""
ui/main_app.py — legacy entry point, now redirects to main_window

This file is kept for backwards compatibility.
The full application lives in ui/main_window.py.
"""
from ui.main_window import main

if __name__ == "__main__":
    main()
