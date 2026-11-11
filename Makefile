PREFIX ?= /usr
DESTDIR ?=
LIB = $(DESTDIR)$(PREFIX)/lib/sunshine-store

install:
	install -Dm644 src/app.py      $(LIB)/app.py
	install -Dm644 src/backend.py  $(LIB)/backend.py
	install -Dm644 src/index.py    $(LIB)/index.py
	install -Dm644 src/themes.py   $(LIB)/themes.py
	install -Dm644 src/windows.py  $(LIB)/windows.py
	install -Dm644 src/appimage.py $(LIB)/appimage.py
	install -dm755 $(DESTDIR)$(PREFIX)/bin
	printf '#!/bin/sh\nexec python3 $(PREFIX)/lib/sunshine-store/app.py "$$@"\n'      > $(DESTDIR)$(PREFIX)/bin/sunshine-store
	printf '#!/bin/sh\nexec python3 $(PREFIX)/lib/sunshine-store/index.py "$$@"\n'    > $(DESTDIR)$(PREFIX)/bin/sunshine-store-index
	printf '#!/bin/sh\nexec python3 $(PREFIX)/lib/sunshine-store/appimage.py "$$@"\n' > $(DESTDIR)$(PREFIX)/bin/sunshine-store-appimage
	printf '#!/bin/sh\nexec python3 $(PREFIX)/lib/sunshine-store/windows.py "$$@"\n' > $(DESTDIR)$(PREFIX)/bin/sunshine-store-windows
	chmod 755 $(DESTDIR)$(PREFIX)/bin/sunshine-store $(DESTDIR)$(PREFIX)/bin/sunshine-store-index $(DESTDIR)$(PREFIX)/bin/sunshine-store-appimage $(DESTDIR)$(PREFIX)/bin/sunshine-store-windows
	install -Dm644 data/sunshine-store.desktop $(DESTDIR)$(PREFIX)/share/applications/sunshine-store.desktop
	install -Dm644 data/sunshine-store.png     $(DESTDIR)$(PREFIX)/share/icons/hicolor/256x256/apps/sunshine-store.png
	install -Dm644 data/sunshine-store-indexer.service $(DESTDIR)$(PREFIX)/lib/systemd/user/sunshine-store-indexer.service
	install -Dm644 data/sunshine-store-indexer.timer   $(DESTDIR)$(PREFIX)/lib/systemd/user/sunshine-store-indexer.timer

.PHONY: install
