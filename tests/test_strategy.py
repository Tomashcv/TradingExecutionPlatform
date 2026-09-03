import unittest

from sp1execution.strategy.robust import Mode, RobustState, crash_target


class StrategyTests(unittest.TestCase):
    def test_targets(self):
        self.assertEqual(crash_target(.299),0); self.assertEqual(crash_target(.30),.10); self.assertEqual(crash_target(.35),.30); self.assertEqual(crash_target(.45),.60); self.assertEqual(crash_target(.50),1.0)
    def test_handoff(self):
        s=RobustState(old_peak=100); self.assertEqual(s.observe_close(70).target_sp500,.10); self.assertEqual(s.observe_close(65).target_sp500,.30); self.assertEqual(s.observe_close(50).target_sp500,1.0); self.assertEqual(s.observe_close(77.5).target_sp500,0); self.assertEqual(s.mode,Mode.POST_HANDOFF); self.assertEqual(s.observe_close(100).event,"REARM_AFTER_OLD_ATH")
if __name__=="__main__": unittest.main()
