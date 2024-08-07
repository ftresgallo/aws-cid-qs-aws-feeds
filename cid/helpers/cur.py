""" Manage AWS CUR
"""
import json
import logging

from cid.base import CidBase
from cid.utils import get_parameter, get_parameters, cid_print
from cid.exceptions import CidCritical

logger = logging.getLogger(__name__)


class CUR(CidBase):
    """ Manage AWS CUR
    """
    cur_minimal_required_columns = [
        'bill_bill_type',
        'bill_billing_entity',
        'bill_billing_period_end_date',
        'bill_billing_period_start_date',
        'bill_invoice_id',
        'bill_payer_account_id',
        'identity_line_item_id',
        'identity_time_interval',
        'line_item_legal_entity',
        'line_item_line_item_description',
        'line_item_line_item_type',
        'line_item_operation',
        'line_item_product_code',
        'line_item_unblended_cost',
        'line_item_usage_account_id',
        'line_item_usage_amount',
        'line_item_usage_end_date',
        'line_item_usage_start_date',
        'line_item_usage_type',
        'pricing_term',
        'pricing_unit',
    ]
    ri_required_columns = [
        'reservation_reservation_a_r_n',
        'reservation_effective_cost',
        'reservation_start_time',
        'reservation_end_time',
        'pricing_lease_contract_length',
        'pricing_offering_class',
        'pricing_purchase_option'
    ]
    sp_required_columns = [
        'savings_plan_savings_plan_a_r_n',
        'savings_plan_savings_plan_effective_cost',
        'savings_plan_start_time',
        'savings_plan_end_time',
        'savings_plan_purchase_term',
        'savings_plan_offering_type',
        'savings_plan_payment_option'
    ]
    _metadata = None

    def __init__(self, athena, glue):
        self.athena = athena
        self.glue = glue

    @property
    def table_name(self) -> str:
        """ Get Athena table name """
        if self.metadata is None:
            raise CidCritical('Error: Cannot detect any CUR table. Hint: Check if AWS Lake Formation is activated on your account, verify that the LakeFormationEnabled parameter is set to yes on the deployment stack')
        return self.metadata.get('Name')

    @property
    def has_resource_ids(self) -> bool:
        """ Return True if CUR has resource ids """
        return 'line_item_resource_id' in self.fields

    @property
    def has_reservations(self) -> bool:
        """ Return True if CUR has reservation fields """
        return all(col in self.fields for col in self.ri_required_columns)

    @property
    def has_savings_plans(self) -> bool:
        """ Return True if CUR has savings plan """
        return all(col in self.fields for col in self.sp_required_columns)

    def get_type_of_column(self, column: str):
        """ Return an Athena type of a given non existent CUR column """
        if column.startswith('cost_category_') or column.startswith('resource_tags_'):
            return 'STRING'
        for ending in ['_cost', '_factor', '_quantity', '_fee', '_amount', '_discount']:
            if column.endswith(ending):
                return 'DOUBLE'
        if column.endswith('_date') and not column.endswith('_to_date'):
            return 'TIMESTAMP'
        special_cases = {
            "reservation_amortized_upfront_cost_for_usage": "DOUBLE",
            "reservation_amortized_upfront_fee_for_billing_period": "DOUBLE",
            "reservation_recurring_fee_for_usage": "DOUBLE",
            "reservation_unused_amortized_upfront_fee_for_billing_period": "DOUBLE",
            "reservation_upfront_value": "DOUBLE",
            "reservation_net_amortized_upfront_cost_for_usage": "DOUBLE",
            "reservation_net_amortized_upfront_fee_for_billing_period": "DOUBLE",
            "reservation_net_recurring_fee_for_usage": "DOUBLE",
            "reservation_net_unused_amortized_upfront_fee_for_billing_period": "DOUBLE",
            "reservation_net_upfront_value": "DOUBLE",
            "savings_plan_total_commitment_to_date": "DOUBLE",
            "savings_plan_savings_plan_rate": "DOUBLE",
            "savings_plan_used_commitment": "DOUBLE",
            "savings_plan_amortized_upfront_commitment_for_billing_period": "DOUBLE",
            "savings_plan_net_amortized_upfront_commitment_for_billing_period": "DOUBLE",
            "savings_plan_recurring_commitment_for_billing_period": "DOUBLE",
        }
        return special_cases.get(column, 'STRING')

    def ensure_column(self, column: str, column_type: str=None):
        """ Ensure column is in the cur. If it is not there - add column """
        column = column.lower()
        if column in [col.get('Name', '').lower() for col in self.metadata.get('Columns', [])]:
            return

        table_can_be_updated = False
        # Check Crawler Behavior - if it has a Configuration/CrawlerOutput/TablesAddOrUpdateBehavior != MergeNewColumns, it will override columns
        crawler_name = self.metadata.get('Parameters', {}).get('UPDATED_BY_CRAWLER')
        if crawler_name:
            try:
                crawler = self.glue.get_crawler(crawler_name)
                config = json.loads(crawler.get('Configuration', '{}'))
                add_or_update = config.get('CrawlerOutput', {}).get('Tables', {}).get('AddOrUpdateBehavior')
                if add_or_update == 'MergeNewColumns':
                    table_can_be_updated = True
            except self.glue.client.exceptions.ClientError as exc:
                logger.debug('Failed to get crawler info')
        if table_can_be_updated:
            column_type = column_type or self.get_type_of_column(column)
            try:
                self.athena.query(f'ALTER TABLE {self.table_name} ADD COLUMNS ({column} {column_type})')
            except (self.athena.client.exceptions.ClientError, CidCritical) as exc:
                raise CidCritical(f'Column {column} is not found in CUR and we were unable to add it. Please check FAQ.') from exc
            # table takes time to update so just adding column to cached data
            self._metadata.get('Columns', []).append({'Name': column, 'Type': column_type})
            cid_print(f"Column '{column}' was added to CUR ({self.table_name}).")
            return

        # if table cannot be updated, check if it is ri/sp case - let's hope dashboard views can handle it:
        if column in self.sp_required_columns + self.ri_required_columns:
            logger.warn(f"Column '{column}' is not present in ({self.table_name}). Will try to continue without.")
            return

        # if a required column is not there and not ri/sp -> stop
        logger.critical(f"Column '{column}' is not in ({self.table_name}). Cannot continue.")


    def table_is_cur(self, table: dict=None, name: str=None, return_reason: bool=False) -> bool:
        """ return True if table metadata fits CUR definition. """
        try:
            table = table or self.athena.get_table_metadata(name)
        except Exception as exc: #pylint: disable=broad-exception-caught
            logger.debug(exc)
            return False if not return_reason else (False, f'cannot get table {name}. {exc}.')

        table_name = table.get('Name')
        columns = [col.get('Name') for col in table.get('Columns')]
        missing_columns = [col for col in self.cur_minimal_required_columns if col not in columns]
        if missing_columns:
            return False if not return_reason else (False, f"Table {table_name} does not contain columns: {','.join(missing_columns)}. You can try ALTER TABLE {table_name} ADD COLUMNS (missing_column string).")

        return True if not return_reason else (True, 'all good')

    @property
    def metadata(self) -> dict:
        """get Athena metadata for the table of CUR """
        if self._metadata:
            return self._metadata

        if get_parameters().get('cur-table-name'):
            table_name = get_parameters().get('cur-table-name')
            try:
                self._metadata = self.athena.get_table_metadata(table_name)
            except self.athena.client.exceptions.ResourceNotFoundException as exc:
                raise CidCritical(f'Provided cur-table-name "{table_name}" is not found. Please make sure the table exists.') from exc
            except self.athena. athena.exceptions.MetadataException as exc:
                raise CidCritical(f'Provided cur-table-name "{table_name}" is not found in database "{self.athena.DatabaseName}". Please make sure the table exists. Also if you use LakeFormation, make sure that the user is granted with read rights.') from exc
            res, message = self.table_is_cur(table=self._metadata, return_reason=True)
            if not res:
                raise CidCritical(f'Table {table_name} does not look like CUR. {message}')
        else:
            # Look all tables and filter ones with CUR fields
            all_tables = self.athena.list_table_metadata()
            if not all_tables:
                logger.warning(
                    f'No tables found in Athena Database {self.athena.DatabaseName} in {self.athena.region}.'
                    f' (Hint: If you see tables in this Database, please check AWS Lake Formation permissions)'
                )
            cur_tables = [tab for tab in all_tables if self.table_is_cur(table=tab)]

            if not cur_tables:
                raise CidCritical(f'CUR table not found. (scanned {len(all_tables)} tables in Athena Database {self.athena.DatabaseName} in {self.athena.region}). But none has required fields: {self.cur_minimal_required_columns}.')

            choices = sorted([tab.get('Name') for tab in cur_tables], reverse=True)

            answer =  get_parameter(
                param_name='cur-table-name',
                message="Please select CUR",
                choices=choices + ['<CREATE CUR TABLE AND CRAWLER>'],
            )
            if answer == '<CREATE CUR TABLE AND CRAWLER>':
                raise CidCritical('CUR creation was requested') # to be captured in common.py
            self._metadata = self.athena.get_table_metadata(answer)
        return self._metadata

    @property
    def fields(self) -> list:
        """get CUR fields """
        return [col.get('Name') for col in self.metadata.get('Columns', [])]

    @property
    def tag_and_cost_category_fields(self) -> list:
        """ Returns all tags and cost category fields. """
        return [field for field in self.fields if field.startswith('resource_tags_user_') or field.startswith('cost_category_')]
