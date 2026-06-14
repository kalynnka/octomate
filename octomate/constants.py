"""Cross-cutting constants shared across layers.

Keeping names like the send tool's here lets the capability that defines the tool
and the schema projection that recognizes it agree on one value without either
layer importing the other.
"""

SEND_TOOL_NAME = "send_message"
