"""Entry point of the packaged desktop app (PyInstaller)."""
import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()
    from cctv_analysis.app.__main__ import main
    main()
