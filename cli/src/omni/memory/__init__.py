"""File/SQLite-backed 5-layer memory (M1–M5) + lab notebook.

Layers:
  M1 session · M2 task · M3 episodic · M4 semantic · M5 artifact

(The HelixForge M0 scratchpad and M6 evolution layers were never written in the
CLI and have been dropped; ``idea_evolution`` remains a *memory_type*, not a layer.)
"""

from omni.memory.service import MemoryLayer, MemoryService

__all__ = ["MemoryService", "MemoryLayer"]
