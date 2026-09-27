"""PumpGrok research and paper-trading layer.

Agents do not import this package to sign. It stores candidates, runs a
deterministic risk gate, and simulates fills. Live sends are outside this
package.
"""

__all__ = ["config", "db", "risk", "paper"]
