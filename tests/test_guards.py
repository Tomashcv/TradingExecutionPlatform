import unittest

from sp1execution.execution.guards import validate_target_weights


class GuardTests(unittest.TestCase):
    def test_ok(self): validate_target_weights(.9,.1)
    def test_bad(self):
        with self.assertRaises(ValueError): validate_target_weights(.8,.3)
if __name__=="__main__": unittest.main()
