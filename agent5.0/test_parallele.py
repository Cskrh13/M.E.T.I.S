import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from grand_planificateur import diagnostic_parallele as dp

class T(unittest.TestCase):
    def test_cas(self):
        self.assertIsNone(dp(None, 0)); self.assertIsNone(dp(None, 1))            # un seul agent : rien à dire
        self.assertIsNone(dp(3, 3)); self.assertIsNone(dp(8, 3))                   # réglage suffisant
        m = dp(None, 3); self.assertIn("n'est pas défini", m); self.assertIn("OLLAMA_NUM_PARALLEL = 3", m)
        m = dp(2, 4); self.assertIn("réglé sur 2", m); self.assertIn("setx OLLAMA_NUM_PARALLEL 4", m)
if __name__ == "__main__": unittest.main()
