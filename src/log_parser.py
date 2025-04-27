#!/usr/bin/env python3
import re
import json
import argparse
import sys

# Function to parse the data and return JSON format
def parse_prices(lines):
    data = []

    for line in lines:
        # Extract timestamp and JSON-like string using regex
        match = re.match(r'Prices updated at (.*?) UTC -> (.*)', line.strip())
        if match:
            ts, prices_str = match.groups()
            try:
                # Safely evaluate the JSON-like string to a Python dict
                prices = json.loads(prices_str.replace("'", '"'))
                data.append({'ts': ts, 'price': prices})
            except json.JSONDecodeError:
                # Skip lines that can't be parsed as JSON
                continue

    return data

# Main script execution
def main():
    parser = argparse.ArgumentParser(description='Parse log data into JSON format.')
    parser.add_argument('-f', '--file', type=str, help='File to parse (default: stdin)')
    args = parser.parse_args()

    if args.file:
        with open(args.file, 'r') as file:
            lines = file.readlines()
    else:
        lines = sys.stdin.readlines()

    parsed_data = parse_prices(lines)
    print(json.dumps(parsed_data, indent=2))

if __name__ == '__main__':
    main()
