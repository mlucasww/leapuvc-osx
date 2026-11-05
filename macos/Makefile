# Run these on the Sequoia test Mac, from the macos/ folder.

SWIFTC ?= swiftc
PYTHON ?= python3
VENV   ?= .venv

.PHONY: all setup leapcap test probe clean

all: leapcap

setup:
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/pip install -r requirements.txt

leapcap: build/leapcap

build/leapcap: leapcap/leapcap.swift
	mkdir -p build
	$(SWIFTC) -O -o build/leapcap leapcap/leapcap.swift

test:
	$(VENV)/bin/python -m pytest tests

probe: build/leapcap
	$(VENV)/bin/python tools/probe_leap.py

clean:
	rm -rf build
