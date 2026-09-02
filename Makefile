# pubdump — .pub to JSON event stream.
#
# macOS (Homebrew) and Windows (MSYS2 UCRT64) are both supported. libmspub
# is only packaged for MSYS2's ucrt64/clang64 repos, not mingw64, so build
# from a UCRT64 shell on Windows.

UNAME_S := $(shell uname -s)
PKGS    := libmspub-0.1 librevenge-0.0 librevenge-stream-0.0

ifeq ($(UNAME_S),Darwin)
  # icu4c is keg-only in Homebrew, so its pkgconfig dir must be added.
  ICU_PC    := $(shell brew --prefix icu4c@78)/lib/pkgconfig
  PKGCONFIG := PKG_CONFIG_PATH=$(ICU_PC):$$PKG_CONFIG_PATH pkg-config
  BIN       := bin/pubdump
else
  PKGCONFIG := pkg-config
  ifneq (,$(findstring MINGW,$(UNAME_S))$(findstring MSYS,$(UNAME_S))$(findstring CYGWIN,$(UNAME_S)))
    BIN := bin/pubdump.exe
    # Fold the GCC runtime into the binary so only the libmspub/librevenge
    # DLLs need shipping alongside it.
    EXTRA_LDFLAGS := -static-libgcc -static-libstdc++
  else
    BIN := bin/pubdump
  endif
endif

CXX      ?= c++
CXXFLAGS += -std=c++17 -O2 -Wall -Wextra $(shell $(PKGCONFIG) --cflags $(PKGS))
LDLIBS   += $(shell $(PKGCONFIG) --libs $(PKGS))

PYTHON ?= python3

.PHONY: all clean dlls test gui
all: $(BIN)

# Standard library only, matching the runtime's own constraint. The tests
# that need the parser skip themselves when bin/pubdump is not built.
test:
	$(PYTHON) -m unittest discover -s tests -t . -v

$(BIN): src/pubdump.cpp
	@mkdir -p bin
	$(CXX) $(CXXFLAGS) -o $@ $< $(LDLIBS) $(EXTRA_LDFLAGS)

# Copy the MinGW DLLs pubdump.exe needs into bin/, transitively, so the
# folder is self-contained before PyInstaller bundles it.
dlls: $(BIN)
	@sh tools/collect-dlls.sh $(BIN) $(UCRT_BIN)

UCRT_BIN ?= /ucrt64/bin

clean:
	rm -rf bin

# The windowed build. Requires pyinstaller and, on macOS, a Python built
# with Tk (brew install python-tk).
gui:
	$(PYTHON) -m PyInstaller pub2idml-gui.spec
