"""Run one scheduler tick, suitable for an external scheduler / Cloud Run Job."""

from app.scheduler import tick

if __name__ == "__main__":
    tick()
