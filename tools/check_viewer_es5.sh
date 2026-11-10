#!/bin/sh
# Parse the viewer's script as strict ES5, the newest JavaScript the Switch's
# browser understands. Needs Node.js (npx fetches the acorn parser).
set -e
cd "$(dirname "$0")/.."
out="${TMPDIR:-/tmp}/switchdisplay-viewer.$$.js"
trap 'rm -f "$out"' EXIT
python3 -c '
import switchdisplay
page = switchdisplay.render_viewer(True, "s", True, True).decode()
print(page[page.index("<script>") + 8:page.index("</script>")])
' > "$out"
npx --yes acorn@8.14.0 --ecma5 --silent "$out"
echo "viewer script parses as ES5"
