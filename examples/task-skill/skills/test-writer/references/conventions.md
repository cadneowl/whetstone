# Test conventions

- Put tests in `test_<module>.py` next to the module.
- One `test_` function per behaviour, named for the behaviour, not the method.
- Assert on the error *type* for failure paths, using `pytest.raises`.
