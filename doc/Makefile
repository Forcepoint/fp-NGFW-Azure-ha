
# Makefile to generate user doc pdf.

# requirements:
# apt install pandoc weasyprint

# note: css modified and obtained from https://gist.github.com/dashed/6714393

CSS=./github-pandoc.css
SRC=./user_guide.md
PDF=./user_guide.pdf

pdf:
	pandoc  --toc  --pdf-engine weasyprint  --self-contained --css $(CSS) $(SRC) -o $(PDF)

preview:
	xdg-open $(PDF)

html:
	pandoc  --toc --self-contained --css $(CSS) $(SRC) -o ./user_guide.html
	xdg-open ./user_guide.html


# use this if weasyprint is not available. table of content won't work though
pdf_fallback:
	pandoc -t html5  --self-contained --css $(CSS) $(SRC) -o $(PDF)
	xdg-open $(PDF)

clean:
	- rm $(PDF) user_guide.html
