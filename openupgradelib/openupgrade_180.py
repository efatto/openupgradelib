# Copyright 2025 Hunki Enterprises BV
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
import logging

from psycopg2 import sql

from .openupgrade import logged_query
from .openupgrade import column_exists
from .openupgrade_tools import table_exists

logger = logging.getLogger("OpenUpgrade")
logger.setLevel(logging.DEBUG)


def convert_company_dependent(
    env, model_name, field_name, value_expression=None, old_field_id=None
):
    """
    Company dependent fields used to live in ir.property, in v18 they are jsonb
    dictionaries with company id as key and the company specific value as value.

    Default values are set in ir.default

    :param model_name: the name of the model
    :param field_name: the name of the field
    :param value_expression: an SQL expression extracting the value to be used
        from a row of ir_property
    :param old_field_id: in case a field has been renamed during the migration,
        pass the field id of the previous field here
    """

    if not table_exists(env.cr, "ir_property"):
        # If you are migrating using the enterprise service, the table
        # ir_property is already converted and deleted. Also, the conversion
        # to company_dependent is done for all the fields in that service too.
        # So when trying to access the table it raises an exception.
        # So adding a condition to ensure this table till exists and being
        # able to do the migration with both services!
        # Reference: https://github.com/OCA/purchase-workflow/pull/2861
        return
    Model = env[model_name]
    Field = (
        env["ir.model.fields"]._get(model_name, field_name)
        if not old_field_id
        else env["ir.model.fields"].browse(old_field_id)
    )
    value_expression = value_expression or (
        "value_%s"
        % {
            "float": "float",
            "boolean": "integer::boolean",
            "integer": "integer",
            "date": "datetime",
            "datetime": "datetime",
        }.get(Field.ttype, "text")
        if Field.ttype != "many2one"
        else "SPLIT_PART(value_reference, ',', 2)::integer"
    )

    logged_query(
        env.cr,
        f"ALTER TABLE {Model._table} ADD COLUMN IF NOT EXISTS {field_name} jsonb",
    )

    logged_query(
        env.cr,
        f"""
        UPDATE {Model._table} SET {field_name}=ir_property_by_company.value
        FROM (
            SELECT
            SPLIT_PART(res_id, ',', 2)::integer res_id,
            JSON_OBJECT_AGG(company_id, {value_expression}) AS "value"
            FROM ir_property
            WHERE
            fields_id={old_field_id or Field.id} AND res_id IS NOT NULL
            AND company_id IS NOT NULL
            GROUP BY res_id
        ) ir_property_by_company
        WHERE {Model._table}.id=ir_property_by_company.res_id
        """,
    )

    env.cr.execute(
        f"""
        SELECT company_id, {value_expression} FROM ir_property
        WHERE
        fields_id={old_field_id or Field.id} AND res_id IS NULL
        """
    )
    for company_id, value in env.cr.fetchall():
        env["ir.default"].set(model_name, field_name, value, company_id=company_id)


def convert_column_to_company_dependent(env, model_name, field_name):
    """Turn a plain stored column into a company dependent jsonb column.

    Reusable helper for a field that becomes ``company_dependent=True`` in
    17.0/18.0 without having lived in ``ir_property`` before (openupgradelib's
    ``convert_to_company_dependent`` only handles the ``ir_property`` storage
    removed in 17.0). Safe to run more than once: it does nothing if the
    column is missing or already ``jsonb``.

    :param env: Odoo environment
    :param model_name: name of the model owning the field
    :param field_name: name of the field/column to convert
    """
    cr = env.cr
    table = env[model_name]._table
    if not column_exists(cr, table, field_name):
        # the column does not exist yet: Odoo will create it as jsonb
        return
    cr.execute(
        """
        SELECT data_type
          FROM information_schema.columns
         WHERE table_name = %s
           AND column_name = %s
        """,
        (table, field_name),
    )
    if cr.fetchone()[0] == "jsonb":
        # already company dependent, nothing to do
        return
    cr.execute("SELECT id FROM res_company ORDER BY id")
    company_ids = [str(row[0]) for row in cr.fetchall()]
    if not company_ids:
        logger.warning(
            "%s: no company found, cannot convert %s.%s to company dependent",
            model_name,
            table,
            field_name,
        )
        return
    pairs = sql.SQL(", ").join(
        sql.SQL("{}, {}").format(sql.Literal(company_id), sql.Identifier(field_name))
        for company_id in company_ids
    )
    logged_query(
        cr,
        sql.SQL(
            "ALTER TABLE {table} ALTER COLUMN {column} TYPE jsonb "
            "USING jsonb_build_object({pairs})"
        ).format(
            table=sql.Identifier(table),
            column=sql.Identifier(field_name),
            pairs=pairs,
        ),
    )
