SCdoc ?= scdoc
MAN_SOURCE := deez.1.scd
BUILD_DIR := target
MANPAGE := $(BUILD_DIR)/deez.1
PEX_OUTPUT := $(BUILD_DIR)/deez.pex

PEX ?= $(shell command -v pex 2>/dev/null)
ifeq ($(PEX),)
PEX := python3 -m pex
endif

.PHONY: man pex clean-man

man: $(MANPAGE)

$(MANPAGE): $(MAN_SOURCE) | $(BUILD_DIR)
	$(SCdoc) < $< > $@

pex: $(PEX_OUTPUT)

$(PEX_OUTPUT): | $(BUILD_DIR)
	$(PEX) . -e deez:run_entrypoint -o $@

$(BUILD_DIR):
	mkdir -p $(BUILD_DIR)

clean-man:
	rm -f $(MANPAGE)
