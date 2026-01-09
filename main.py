from fastapi import FastAPI, Depends,Body,HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session
from deps import get_db1, get_db2
from collections import defaultdict
import re
from fastapi.responses import PlainTextResponse


# def parse_column(col: str):
#     pattern = r"^([a-zA-Z_][a-zA-Z0-9_]*)\.([a-zA-Z_][a-zA-Z0-9_]*)$"
#     match = re.match(pattern, col)
#     if not match:
#         raise HTTPException(status_code=400, detail=f"Invalid column: {col}")
#     return match.group(1), match.group(2)



app = FastAPI()

IDENTIFIER_PATTERN = r"^[a-zA-Z_][a-zA-Z0-9_]*$"

def validate_identifier(value: str):
    if not re.match(IDENTIFIER_PATTERN, value):
        raise HTTPException(400, f"Invalid identifier: {value}")

def parse_column(col: str):
    """
    Expected format: table.column
    """
    parts = col.split(".")
    if len(parts) != 2:
        raise HTTPException(400, f"Invalid column format: {col}")

    table, column = parts
    validate_identifier(table)
    validate_identifier(column)
    return table, column


FK_QUERY = text("""
    SELECT
        tc.table_name       AS source_table,
        kcu.column_name     AS source_column,
        ccu.table_name      AS target_table,
        ccu.column_name     AS target_column,
        ccu.table_schema    AS target_schema
    FROM information_schema.table_constraints tc
    JOIN information_schema.key_column_usage kcu
        ON tc.constraint_name = kcu.constraint_name
    JOIN information_schema.constraint_column_usage ccu
        ON ccu.constraint_name = tc.constraint_name
    WHERE tc.constraint_type = 'FOREIGN KEY'
      AND tc.table_schema = :schema
""")

MAX_DEPTH = 5

def find_fk_path(graph, start, target, visited=None, depth=0):
    if depth > MAX_DEPTH:
        return None

    if visited is None:
        visited = set()

    if start == target:
        return []

    visited.add(start)

    for fk in graph.get(start, []):
        next_table = fk["target_table"]

        if next_table in visited:
            continue

        path = find_fk_path(
            graph,
            next_table,
            target,
            visited,
            depth + 1
        )
        if path is not None:
            return [fk] + path

    return None

@app.post("/dynamic-select")
def dynamic_select(
    payload: dict = Body(...),
    db1: Session = Depends(get_db1),
    db2: Session = Depends(get_db2)
):
    schema = payload.get("schema")
    base_table = payload.get("table_name")
    columns = payload.get("columns")

    if not schema or not base_table or not columns:
        raise HTTPException(400, "schema, table_name and columns are required")

    validate_identifier(schema)
    validate_identifier(base_table)

    # Load FK metadata
    fk_rows = db2.execute(FK_QUERY, {"schema": schema}).mappings().all()

    # Build FK graph
    fk_graph = defaultdict(list)
    for fk in fk_rows:
        fk_graph[fk["source_table"]].append(fk)

    # Aliases
    alias_map = {base_table: "t0"}
    joins = []
    alias_counter = 1

    def ensure_joins(path):
        nonlocal alias_counter
        current_table = base_table

        for fk in path:
            target = fk["target_table"]

            if target not in alias_map:
                alias_map[target] = f"t{alias_counter}"
                alias_counter += 1

                joins.append(f"""
                    LEFT JOIN {fk['target_schema']}.{target} {alias_map[target]}
                    ON {alias_map[current_table]}.{fk['source_column']}
                       = {alias_map[target]}.{fk['target_column']} and {alias_map[target]}.is_deleted = false
                """)

            current_table = target

    # Build SELECT columns
    select_columns = []

    for col in columns:
        table, column = parse_column(col)

        if table not in alias_map:
            path = find_fk_path(fk_graph, base_table, table)
            if not path:
                raise HTTPException(
                    400,
                    f"No foreign key path found from {base_table} to {table}"
                )
            ensure_joins(path)

        select_columns.append(
            f"{alias_map[table]}.{column} AS {table}_{column}"
        )

    # Final SQL
    sql = f"""
        SELECT {", ".join(select_columns)}
        FROM {schema}.{base_table} t0
        {" ".join(joins)}
        where t0.valid_to is null
    """
    # return sql

    return db1.execute(text(sql)).mappings().all()

@app.post("/insert-from-values")
def insert_from_values(
    payload: dict = Body(...),
    db1: Session = Depends(get_db1),
    db2: Session = Depends(get_db2),
):
    target_schema = payload["target_schema"]
    target_table = payload["target_table"]
    columns = payload["columns"]
    rows = payload["rows"]

    if not rows:
        raise HTTPException(400, "No rows provided")

    # ------------------ Validate identifiers ------------------

    validate_identifier(target_schema)
    validate_identifier(target_table)

    for col in columns:
        validate_identifier(col)

    # ------------------ Build INSERT SQL ------------------

    column_list = ", ".join(columns)
    placeholders = ", ".join([f":{col}" for col in columns])

    sql = f"""
        INSERT INTO {target_schema}.{target_table}
        ({column_list})
        VALUES ({placeholders})
    """

    # ------------------ Prepare VALUES ------------------

    values = []
    for row in rows:
        record = {}
        for col in columns:
            # Find matching value in row (endswith strategy)
            match = next(
                (v for k, v in row.items() if k.endswith(f"_{col}")),
                None
            )
            record[col] = match
        values.append(record)

    # ------------------ Execute BULK INSERT ------------------

    # db.execute(text(sql), values)
    # db.commit()
    # return sql

    return {
        "message": "Insert completed",
        "rows_inserted": len(values)
    }

@app.post("/prepare-insert-sql",  response_class=PlainTextResponse)
def prepare_insert_sql(payload: dict = Body(...)):
    target_schema = payload["target_schema"]
    target_table = payload["target_table"]
    columns = payload["columns"]
    rows = payload["rows"]

    # ------------------ Validation ------------------

    validate_identifier(target_schema)
    validate_identifier(target_table)

    for col in columns:
        validate_identifier(col)

    if not rows:
        raise HTTPException(400, "No rows provided")

    # ------------------ Helper to format values ------------------

    def format_value(value):
        if value is None:
            return "NULL"
        if isinstance(value, (int, float)):
            return str(value)
        # escape single quotes for SQL
        escaped = str(value).replace("'", "''")
        return f"'{escaped}'"

    # ------------------ Build VALUES ------------------

    values_sql = []

    for row in rows:
        row_values = []
        for col in columns:
            # match dynamic-select output keys (endswith strategy)
            value = next(
                (v for k, v in row.items() if k.endswith(f"_{col}")),
                None
            )
            row_values.append(format_value(value))

        values_sql.append(f"({', '.join(row_values)})")

    # ------------------ Final INSERT SQL ------------------

        values_str = ",\n".join(values_sql)

        insert_sql = f"""INSERT INTO {target_schema}.{target_table}
                    (
                     {", ".join(columns)}
                     )
                    VALUES
                    {values_str};
                    """.strip()
    
    text_data = f'''
        DROP SEQUENCE IF EXISTS "ns_default_master_data".{target_table}_rowid_seq CASCADE;
        CREATE SEQUENCE IF NOT EXISTS "ns_default_master_data"."{target_table}_rowid_seq"
        INCREMENT 1
        START 1
        MINVALUE 1
        CACHE 1;

        DROP TABLE IF EXISTS "ns_default_master_data".{target_table} CASCADE;
        CREATE TABLE IF NOT EXISTS "ns_default_master_data".{target_table}
        (
        rid bigint NOT NULL DEFAULT nextval('"ns_default_master_data"."{target_table}_rowid_seq"'::regclass),
        business_unit_code text,
        lob_code text,
        premium_basis_code text,
        premium_basis_descr text,
        currency_symbol text,
        CONSTRAINT {target_table}_pkey PRIMARY KEY (rid) 
        );
        COMMENT ON TABLE "ns_default_master_data"."{target_table}" 
        IS 'Master table for Premium basics';

        COMMENT ON COLUMN "ns_default_master_data"."{target_table}"."rid"
        IS 'Row id - identify a row uniquely';

        COMMENT ON COLUMN "ns_default_master_data"."{target_table}"."business_unit_code" 
        IS 'It contains the business unit code';

        COMMENT ON COLUMN "ns_default_master_data"."{target_table}"."lob_code" 
        IS 'It contains the lob code';

        COMMENT ON COLUMN "ns_default_master_data"."{target_table}"."premium_basis_code" 
        IS 'It contains the premium basis code';

        COMMENT ON COLUMN "ns_default_master_data"."{target_table}"."premium_basis_descr"
        IS 'It contains the premium basis descr';

        COMMENT ON COLUMN "ns_default_master_data"."{target_table}"."currency_symbol" 
        IS 'It contains the currency symbol';

        {insert_sql}
    '''
    

    return text_data


# @app.get("/combine-data")
# def combine_data(
#     payload: dict = Body(...),
#     db1: Session = Depends(get_db1),
#     db2: Session = Depends(get_db2)
# ):
#     schema = payload.get("schema")
#     table_name = payload.get("table_name")
#     columns = payload.get("columns")

#     foreign_keys = db2.execute(
#         text('''SELECT
#     tc.constraint_name,
#     kcu.column_name AS current_table_column,
#     ccu.table_schema AS referenced_schema,
#     ccu.table_name AS referenced_table,
#     ccu.column_name AS referenced_column
# FROM information_schema.table_constraints tc
# JOIN information_schema.key_column_usage kcu
#     ON tc.constraint_name = kcu.constraint_name
# JOIN information_schema.constraint_column_usage ccu
#     ON ccu.constraint_name = tc.constraint_name
# WHERE tc.constraint_type = 'FOREIGN KEY'
#   AND tc.table_schema = :schema
#   AND tc.table_name = :table_name;'''),
#         {"schema": schema, "table_name": table_name}
#     ).mappings().all()
#     print(foreign_keys)

#     base_alias = "t0"
#     alias_map = {table_name: base_alias}
#     joins = []
#     select_cols = []
#     alias_counter = 1

#     for col in columns:
#         table, _ = parse_column(col)

#         if table not in alias_map:
#             fk = next(
#                 (fk for fk in foreign_keys if fk["referenced_table"] == table),
#                 None
#             )

#             if not fk:
#                 raise HTTPException(400, f"No FK for table {table}")

#             alias = f"t{alias_counter}"
#             alias_counter += 1

#             alias_map[table] = alias

#             joins.append(f"""
#                 LEFT JOIN {fk['referenced_schema']}.{table} {alias}
#                 ON {base_alias}.{fk['current_table_column']}
#                    = {alias}.{fk['referenced_column']}
#             """)

#     for col in columns:
#         table, column = parse_column(col)
#         alias = alias_map[table]
#         select_cols.append(f"{alias}.{column} AS {table}_{column}")

#     sql = f"""
#     SELECT {", ".join(select_cols)}
#     FROM {schema}.{table_name} {base_alias}
#     {" ".join(joins)}
#     LIMIT 100
#     """

#     data = db1.execute(text(sql)).mappings().all()
#     return data

    # orders = db2.execute(
    #     text("SELECT id, name FROM usermgmt.users LIMIT 5")
    # ).mappings().all()

    # return {
    #     "users_db1": users,
    #     "orders_db2": orders
    # }



# INPUT_CSV = "state.csv"
# OUTPUT_SQL = "rate_prop_brush_zone_factor.sql"
# TABLE_NAME = '"ns_default_master_data"."rate_prop_brush_zone_factor"'

# with open(INPUT_CSV, newline="", encoding="utf-8") as csvfile, \
#      open(OUTPUT_SQL, "w", encoding="utf-8") as sqlfile:

#     reader = csv.reader(csvfile)

#     sqlfile.write(f"""INSERT INTO {TABLE_NAME} (
#   business_unit_name,
#   state_name,
#   state_code,
#   brush_zone_code,
#   brush_zone_descr,
#   building_construction_code,
#   building_construction_descr,
#   factor
# ) VALUES
# """)

#     rows = []
#     for row in reader:
#         state_name, state_code, bz_code, bz_descr, bc_code, bc_descr, bu_name, factor = row

#         rows.append(
#             f"('{bu_name}','{state_name}','{state_code}','{bz_code}','{bz_descr}',"
#             f"'{bc_code}','{bc_descr}',{factor})"
#         )

#     sqlfile.write(",\n".join(rows))
#     sqlfile.write(";\n")

# print("PostgreSQL INSERT file generated successfully.")

