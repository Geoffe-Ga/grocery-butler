"""Tests for grocery_butler.main module."""

from grocery_butler.main import main


def test_main_runs() -> None:
    """Test that main() runs without error."""
    main()  # Should print "Hello from grocery-butler!"
