PREFIX ?= /usr
DESTDIR ?=

install:
	install -Dm644 src/app.py     $(DESTDIR)$(PREFIX)/lib/sunshine-store/app.py
	install -Dm644 src/backend.py $(DESTDIR)$(PREFIX)/lib/sunshine-store/backend.py
	install -dm755 $(DESTDIR)$(PREFIX)/bin
	printf '#!/bin/sh\nexec python3 $(PREFIX)/lib/sunshine-store/app.py "$$@"\n' > $(DESTDIR)$(PREFIX)/bin/sunshine-store
	chmod 755 $(DESTDIR)$(PREFIX)/bin/sunshine-store
	install -Dm644 data/sunshine-store.desktop $(DESTDIR)$(PREFIX)/share/applications/sunshine-store.desktop
	install -Dm644 data/sunshine-store.png $(DESTDIR)$(PREFIX)/share/icons/hicolor/128x128/apps/sunshine-store.png

.PHONY: install
