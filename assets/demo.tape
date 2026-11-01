# Regenerate with: vhs assets/demo.tape  (run from repo root, with .venv installed)
Output assets/demo.gif
Set Shell "bash"
Set FontSize 15
Set Width 1200
Set Height 640
Set Padding 18
Set Theme "Catppuccin Mocha"
Set TypingSpeed 35ms

Hide
Type `export PATH=$PWD/.venv/bin:$PATH PS1="$ " && clear` Enter
Sleep 300ms
Show

Type "# Agent wants to edit build_edit_context() in a 1,264-line file" Enter
Sleep 600ms
Type "# Without code-graph: Read the whole file" Enter
Type `wc -l src/code_graph/context_builder.py; echo "~$(( $(wc -c < src/code_graph/context_builder.py) / 4 )) tokens"` Enter
Sleep 2s
Type "# With code-graph: one surgical call" Enter
Type "code-graph edit-context build_edit_context -p . | head -28" Enter
Sleep 4500ms
Type `code-graph edit-context build_edit_context -p . | echo "~$(( $(wc -c) / 4 )) tokens"` Enter
Sleep 2500ms
Type "# Safe to rename? Check every caller first" Enter
Type "code-graph check build_edit_context -p . | head -12" Enter
Sleep 4s
