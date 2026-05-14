SCdoc ?= scdoc
MAN_SOURCE := deez.1.scd
BUILD_DIR := target
MANPAGE := $(BUILD_DIR)/deez.1

.PHONY: man clean-man

man: $(MANPAGE)

$(MANPAGE): $(MAN_SOURCE) | $(BUILD_DIR)
	$(SCdoc) < $< > $@

$(BUILD_DIR):
	mkdir -p $(BUILD_DIR)

clean-man:
	rm -f $(MANPAGE)
