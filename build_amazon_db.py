import argparse
import csv
import html
import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path


ALIASES = {
    "product_id": ("product_id", "asin", "sku", "item_id"),
    "product_name": ("product_name", "product_title", "title", "item_name", "product"),
    "category": ("category", "product_category", "department"),
    "brand": ("brand", "manufacturer"),
    "price": ("discounted_price", "sale_price", "price", "unit_price", "actual_price"),
    "rating": ("rating", "stars", "average_rating"),
    "review_count": ("rating_count", "review_count", "reviews", "number_of_reviews"),
    "units_sold": ("units_sold", "quantity_sold", "sales_quantity", "quantity"),
    "revenue": ("revenue", "sales", "total_sales", "sales_amount"),
    "date": ("date", "order_date", "review_date", "snapshot_date", "date_added"),
}


def normalize_name(value):
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def parse_number(value):
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text or text.lower() in {"nan", "none", "null", "n/a", "na"}:
        return None
    text = text.replace("$", "").replace("£", "").replace("€", "")
    text = text.replace("₹", "").replace("%", "").strip()
    try:
        return float(text)
    except ValueError:
        return None


def read_source(path):
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        sample = source.read(8192)
        source.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",	;|")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(source, dialect=dialect)
        if not reader.fieldnames:
            raise ValueError("The input CSV must have a header row.")
        rows = list(reader)
    if not rows:
        raise ValueError("The input CSV has headers but no data rows.")
    return reader.fieldnames, rows


def resolve_columns(fieldnames):
    by_normalized_name = {normalize_name(field): field for field in fieldnames}
    resolved = {}
    for canonical, aliases in ALIASES.items():
        for alias in aliases:
            if alias in by_normalized_name:
                resolved[canonical] = by_normalized_name[alias]
                break
    if "product_name" not in resolved and "product_id" not in resolved:
        raise ValueError(
            "Could not identify a product column. Include a header such as "
            "product_name, title, product_id, or asin."
        )
    return resolved


def clean_rows(rows, columns):
    cleaned = []
    for row_number, source_row in enumerate(rows, start=2):
        product_name = source_row.get(columns.get("product_name", ""), "")
        product_id = source_row.get(columns.get("product_id", ""), "")
        product_name = (product_name or product_id or "Unnamed product").strip()
        product_id = (product_id or "").strip()
        key = product_id or normalize_name(product_name)
        values = {name: source_row.get(column, "") for name, column in columns.items()}
        cleaned.append({
            "source_row": row_number,
            "product_key": key,
            "product_id": product_id,
            "product_name": product_name,
            "category": (values.get("category") or "").strip(),
            "brand": (values.get("brand") or "").strip(),
            "price": parse_number(values.get("price")),
            "rating": parse_number(values.get("rating")),
            "review_count": parse_number(values.get("review_count")),
            "units_sold": parse_number(values.get("units_sold")),
            "revenue": parse_number(values.get("revenue")),
            "date": (values.get("date") or "").strip(),
            "raw": source_row,
        })
    return cleaned


def average(values):
    available = [value for value in values if value is not None]
    return sum(available) / len(available) if available else None


def build_products(rows):
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["product_key"]].append(row)
    products = []
    product_keys = {}
    for product_key, observations in grouped.items():
        first = observations[0]
        product = {
            "product_key": product_key,
            "product_id": first["product_id"],
            "product_name": first["product_name"],
            "category": next((row["category"] for row in observations if row["category"]), ""),
            "brand": next((row["brand"] for row in observations if row["brand"]), ""),
            "avg_price": average([row["price"] for row in observations]),
            "avg_rating": average([row["rating"] for row in observations]),
            "avg_review_count": average([row["review_count"] for row in observations]),
            "observation_count": len(observations),
        }
        products.append(product)
        product_keys[product_key] = len(products)
    return products, product_keys


def write_csv(path, fieldnames, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def create_database(path, rows, products):
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            DROP TABLE IF EXISTS fact_observations;
            DROP TABLE IF EXISTS dim_product;
            DROP TABLE IF EXISTS raw_amazon_rows;
            CREATE TABLE dim_product (
                product_key TEXT PRIMARY KEY,
                product_id TEXT,
                product_name TEXT NOT NULL,
                category TEXT,
                brand TEXT,
                avg_price REAL,
                avg_rating REAL,
                avg_review_count REAL,
                observation_count INTEGER NOT NULL
            );
            CREATE TABLE fact_observations (
                observation_id INTEGER PRIMARY KEY,
                product_key TEXT NOT NULL REFERENCES dim_product(product_key),
                source_row INTEGER NOT NULL,
                observation_date TEXT,
                price REAL,
                rating REAL,
                review_count REAL,
                units_sold REAL,
                revenue REAL
            );
            CREATE TABLE raw_amazon_rows (
                source_row INTEGER PRIMARY KEY,
                row_json TEXT NOT NULL
            );
            CREATE INDEX idx_observations_product ON fact_observations(product_key);
            CREATE INDEX idx_products_category ON dim_product(category);
        """)
        connection.executemany(
            "INSERT INTO dim_product VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [tuple(product[field] for field in (
                "product_key", "product_id", "product_name", "category", "brand",
                "avg_price", "avg_rating", "avg_review_count", "observation_count"
            )) for product in products],
        )
        connection.executemany(
            "INSERT INTO fact_observations "
            "(product_key, source_row, observation_date, price, rating, review_count, units_sold, revenue) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(row["product_key"], row["source_row"], row["date"], row["price"],
              row["rating"], row["review_count"], row["units_sold"], row["revenue"])
             for row in rows],
        )
        connection.executemany(
            "INSERT INTO raw_amazon_rows VALUES (?, ?)",
            [(row["source_row"], json.dumps(row["raw"], ensure_ascii=False)) for row in rows],
        )
    connection.close()


def write_bar_chart(path, title, values, color):
    values = [(label, value) for label, value in values if value is not None]
    values = sorted(values, key=lambda item: item[1], reverse=True)[:12]
    if not values:
        return False
    width, row_height = 1000, 34
    left, right = 300, 930
    height = 100 + row_height * len(values)
    maximum = max(value for _, value in values) or 1
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#fbfaf7"/>',
        f'<text x="36" y="42" font-family="Arial,sans-serif" font-size="23" font-weight="700" fill="#202b2b">{html.escape(title)}</text>',
    ]
    for index, (label, value) in enumerate(values):
        y = 72 + index * row_height
        bar_width = max(2, (right - left) * value / maximum)
        display_label = label if len(label) <= 38 else label[:35] + "..."
        parts.extend([
            f'<text x="{left - 12}" y="{y + 16}" text-anchor="end" font-family="Arial,sans-serif" font-size="12" fill="#354343">{html.escape(display_label)}</text>',
            f'<rect x="{left}" y="{y}" width="{bar_width:.1f}" height="21" rx="2" fill="{color}"/>',
            f'<text x="{min(left + bar_width + 8, right + 8):.1f}" y="{y + 16}" font-family="Arial,sans-serif" font-size="12" fill="#354343">{value:,.2f}</text>',
        ])
    parts.append("</svg>")
    path.write_text("\n".join(parts), encoding="utf-8")
    return True


def create_visualizations(output_dir, products, rows):
    metric_specs = [
        ("avg_price", "Average price by product", "#d95d39"),
        ("avg_rating", "Average rating by product", "#277da1"),
        ("avg_review_count", "Average review count by product", "#43aa8b"),
    ]
    if any(row["units_sold"] is not None for row in rows):
        metric_specs.append(("units_sold", "Units sold by product", "#f9c74f"))
    if any(row["revenue"] is not None for row in rows):
        metric_specs.append(("revenue", "Revenue by product", "#9b5de5"))

    created = []
    for field, title, color in metric_specs:
        if field in {"avg_price", "avg_rating", "avg_review_count"}:
            data = [(product["product_name"], product[field]) for product in products]
        else:
            totals = defaultdict(float)
            for row in rows:
                if row[field] is not None:
                    totals[row["product_key"]] += row[field]
            names = {product["product_key"]: product["product_name"] for product in products}
            data = [(names[key], value) for key, value in totals.items()]
        filename = field + ".svg"
        if write_bar_chart(output_dir / filename, title, data, color):
            created.append(filename)
    return created


def run(input_path, output_dir):
    fieldnames, source_rows = read_source(input_path)
    columns = resolve_columns(fieldnames)
    rows = clean_rows(source_rows, columns)
    products, _ = build_products(rows)
    output_dir.mkdir(parents=True, exist_ok=True)

    create_database(output_dir / "amazon_products.sqlite", rows, products)
    write_csv(output_dir / "dim_product.csv", list(products[0]), products)
    fact_fields = ["product_key", "source_row", "date", "price", "rating", "review_count", "units_sold", "revenue"]
    write_csv(output_dir / "fact_observations.csv", fact_fields, rows)
    write_csv(output_dir / "raw_amazon_rows.csv", fieldnames, source_rows)
    charts = create_visualizations(output_dir, products, rows)

    print(f"Input rows: {len(rows):,}")
    print(f"Distinct products: {len(products):,}")
    print(f"SQLite database: {output_dir / 'amazon_products.sqlite'}")
    print("Power BI tables: dim_product.csv, fact_observations.csv")
    print("Top products by average rating:")
    rated = sorted(
        (product for product in products if product["avg_rating"] is not None),
        key=lambda product: product["avg_rating"], reverse=True,
    )[:10]
    for product in rated:
        print(f"  {product['product_name']} | rating {product['avg_rating']:.2f} | category {product['category'] or 'not provided'}")
    print("Charts created: " + (", ".join(charts) if charts else "no numeric product measures found"))
    print("Power BI model: relate dim_product[product_key] 1-to-many to fact_observations[product_key].")


def main():
    parser = argparse.ArgumentParser(
        description="Build an Amazon product database, Power BI tables, and SVG charts from a CSV export."
    )
    parser.add_argument("input_csv", type=Path, help="Path to the Amazon raw-data CSV")
    parser.add_argument("--output", type=Path, default=Path("amazon_output"), help="Output folder (default: amazon_output)")
    args = parser.parse_args()
    if not args.input_csv.is_file():
        parser.error(f"input CSV not found: {args.input_csv}")
    try:
        run(args.input_csv, args.output)
    except (OSError, ValueError, sqlite3.Error) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
