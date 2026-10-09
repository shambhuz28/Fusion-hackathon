"""Convert a saved CelesTrak satellite table page (table.php, saved from a browser) to CSV.

The page holds summary columns only (period, inclination, apogee, perigee,
eccentricity, GP age). It lacks the full element set SGP4 needs, so use
fetch_positions.py for positions; this CSV is the full list of objects.

Usage:
    python src/table_html_to_csv.py "CelesTrak_ Active Satellites.html" data/active_satellites_table.csv
"""

import csv
import sys
from html.parser import HTMLParser

COLUMNS = [
    "intl_designator", "norad_id", "name", "period_min", "inclination_deg",
    "apogee_km", "perigee_km", "eccentricity", "gp_age_days",
]
# Cell index in the page's table -> output column; cell 8 ("Latest Data") holds only links.
CELL_TO_COLUMN = {0: 0, 1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 7: 7, 9: 8}


class SatelliteTableParser(HTMLParser):
    """Collects the text of each <td> in rows of the table with id="tableID"."""

    def __init__(self):
        super().__init__()
        self.in_table = False
        self.row = None
        self.cell = None
        self.rows = []

    def handle_starttag(self, tag, attrs):
        if tag == "table" and dict(attrs).get("id") == "tableID":
            self.in_table = True
        elif self.in_table and tag == "tr":
            self.row = []
        elif self.row is not None and tag == "td":
            self.cell = []

    def handle_endtag(self, tag):
        if tag == "table" and self.in_table:
            self.in_table = False
        elif tag == "td" and self.cell is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if self.row:
                self.rows.append(self.row)
            self.row = None

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    src, dst = sys.argv[1], sys.argv[2]

    parser = SatelliteTableParser()
    with open(src, encoding="utf-8") as f:
        parser.feed(f.read())

    out_rows, skipped = [], 0
    for cells in parser.rows:
        if len(cells) != 10:
            skipped += 1
            continue
        row = [""] * len(COLUMNS)
        for cell_index, col_index in CELL_TO_COLUMN.items():
            value = cells[cell_index]
            # Numeric columns use thousands separators on the page ("1,074").
            row[col_index] = value.replace(",", "") if col_index >= 3 else value
        out_rows.append(row)

    with open(dst, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(COLUMNS)
        writer.writerows(out_rows)
    print(f"Wrote {len(out_rows)} satellites to {dst} ({skipped} rows skipped)")


if __name__ == "__main__":
    main()
