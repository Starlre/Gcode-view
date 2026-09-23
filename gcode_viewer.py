"""Open the offline G-code visual simulator in the default browser."""
from pathlib import Path
import webbrowser


def main() -> None:
    page = Path(__file__).with_name("GcodeViewer.html")
    webbrowser.open(page.as_uri())


if __name__ == "__main__":
    main()
