# Copyright 2014 SAP SE
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import collections
from pyhdb.protocol.message import RequestMessage
from pyhdb.protocol.segments import RequestSegment
from pyhdb.protocol.types import escape_values, by_type_code
from pyhdb.protocol.parts import Command, FetchSize, ResultSetId, StatementId, Parameters
from pyhdb.protocol.constants import message_types, function_codes, part_kinds
from pyhdb.exceptions import ProgrammingError, InterfaceError, DatabaseError
from pyhdb.compat import iter_range


FORMAT_OPERATION_ERRORS = [
    'not enough arguments for format string',
    'not all arguments converted during string formatting'
]


def format_operation(operation, parameters=None):
    if parameters is not None:
        e_values = escape_values(parameters)
        try:
            operation = operation % e_values
        except TypeError, msg:
            if str(msg) in FORMAT_OPERATION_ERRORS:
                # Python DBAPI expects a ProgrammingError in this case
                raise ProgrammingError(str(msg))
            else:
                # some other error message appeared, so just reraise exception:
                raise
    return operation


class PreparedStatement(object):
    """Reference object to a prepared statement including parameter (meta) data"""

    ParamTuple = collections.namedtuple('Parameter', 'id type_code length value')

    def __init__(self, connection, statement_id, params_metadata, result_metadata_part):
        """Initialize PreparedStatement part object
        :param connection: connection object
        :param statement_id: 8-byte statement identifier
        :param params_metadata: A tuple of named-tuple instances containing parameter meta data:
               Example: (ParameterMetadata(options=2, datatype=26, mode=1, id=0, length=24, fraction=0),)
        :param result_metadata_part: can be None
        """

        self._connection = connection
        self.statement_id = statement_id
        self._params_metadata = params_metadata
        self.result_metadata_part = result_metadata_part
        self._pushed_row_params = []
        self._multi_row_parameters = None
        self.end_of_data = False

    def prepare_parameters(self, multi_row_parameters):
        """ Attribute sql parameters with meta data for a prepared statement.
        Make some basic checks that at least the number of parameters is correct.
        :param multi_row_parameters: A list/tuple containing list/tuples of parameters (for multiple rows)
        :returns: A generator producing parameters attributed with meta data for one sql statement (a row) at a time
        """
        self.end_of_data = False
        self._multi_row_parameters = iter(multi_row_parameters)
        return self

    def __iter__(self):
        return self

    def __nonzero__(self):
        return not self.end_of_data

    def next(self):
        if self.end_of_data:
            raise StopIteration()
        if self._pushed_row_params:
            row_params = self._pushed_row_params.pop()
        else:
            try:
                parameters = self._multi_row_parameters.next()
            except StopIteration:
                self.end_of_data = True
                raise
            if not isinstance(parameters, (list, tuple)):
                raise ProgrammingError("Prepared statement parameters supplied as %s, shall be list or tuple." %
                                       str(type(parameters)))

            if len(parameters) != len(self._params_metadata):
                raise ProgrammingError("Prepared statement parameters expected %d supplied %d." %
                                       (len(self._params_metadata), len(parameters)))
            row_params = [self.ParamTuple(p.id, p.datatype, p.length, parameters[p.id]) for p in self._params_metadata]
        return row_params

    def push_back(self, row_params):
        self._pushed_row_params.append(row_params)


class Cursor(object):
    """Database cursor class"""
    def __init__(self, connection):
        self.connection = connection
        self._buffer = collections.deque()
        self._received_last_resultset_part = False
        self._executed = None

        self.rowcount = -1
        self.description = None
        self.rownumber = None
        self.arraysize = 1
        self._prepared_statements = {}

    @property
    def prepared_statement_ids(self):
        return self._prepared_statements.keys()

    def get_prepared_statement(self, statement_id):
        return self._prepared_statements[statement_id]

    def prepare(self, statement):
        """Prepare SQL statement in HANA and cache it
        :param statement; a valid SQL statement
        :returns: statement_id (of prepared and cached statement)
        """
        self._check_closed()

        request = RequestMessage.new(
            self.connection,
            RequestSegment(
                message_types.PREPARE,
                Command(statement)
            )
        )
        response = self.connection.send_request(request)

        statement_id = params_metadata = result_metadata_part = None

        for part in response.segments[0].parts:
            if part.kind == part_kinds.STATEMENTID:
                statement_id = part.statement_id
            elif part.kind == part_kinds.PARAMETERMETADATA:
                params_metadata = part.values
            elif part.kind == part_kinds.RESULTSETMETADATA:
                result_metadata_part = part

        # Check that both variables have been set in previous loop, we need them:
        assert statement_id is not None
        assert params_metadata is not None
        # cache statement:
        self._prepared_statements[statement_id] = PreparedStatement(self.connection, statement_id,
                                                                    params_metadata, result_metadata_part)
        return statement_id

    def execute_prepared(self, prepared_statement, multi_row_parameters):
        """
        :param prepared_statement: A PreparedStatement instance
        :param multi_row_parameters: A list/tuple containing list/tuples of parameters (for multiple rows)
        """
        self._check_closed()

        # Convert parameters into a generator producing lists with parameters as named tuples (incl. some meta data):
        parameters = prepared_statement.prepare_parameters(multi_row_parameters)

        while parameters:
            request = RequestMessage.new(
                self.connection,
                RequestSegment(
                    message_types.EXECUTE,
                    (StatementId(prepared_statement.statement_id),
                     Parameters(parameters))
                )
            )
            response = self.connection.send_request(request)

            parts = response.segments[0].parts
            function_code = response.segments[0].function_code
            if function_code == function_codes.SELECT:
                self._handle_prepared_select(parts, prepared_statement.result_metadata_part)
            elif function_code in function_codes.DML:
                self._handle_prepared_insert(parts)
            elif function_code == function_codes.DDL:
                # No additional handling is required
                pass
            else:
                raise InterfaceError("Invalid or unsupported function code received")

    def _execute_direct(self, operation):
        """Execute statements which are not going through 'prepare_statement' (aka 'direct execution').
        Either their have no parameters, or Python's string expansion has been applied to the SQL statement.
        :param operation:
        """
        request = RequestMessage.new(
            self.connection,
            RequestSegment(
                message_types.EXECUTEDIRECT,
                Command(operation)
            )
        )
        response = self.connection.send_request(request)

        parts = response.segments[0].parts
        function_code = response.segments[0].function_code
        if function_code == function_codes.SELECT:
            self._handle_select(parts)
        elif function_code in function_codes.DML:
            self._handle_insert(parts)
        elif function_code == function_codes.DDL:
            # No additional handling is required
            pass
        else:
            raise InterfaceError("Invalid or unsupported function code received")

    def execute(self, statement, parameters=None):
        """Execute statement on database
        :param statement: a valid SQL statement
        :param parameters: a list/tuple of parameters
        :returns: this cursor

        In order to be compatible with Python's DBAPI five parameter styles
        must be supported.

        paramstyle 	Meaning
        ---------------------------------------------------------
        1) qmark       Question mark style, e.g. ...WHERE name=?
        2) numeric     Numeric, positional style, e.g. ...WHERE name=:1
        3) named       Named style, e.g. ...WHERE name=:name
        4) format 	   ANSI C printf format codes, e.g. ...WHERE name=%s
        5) pyformat    Python extended format codes, e.g. ...WHERE name=%(name)s

        Hana's 'prepare statement' feature supports 1) and 2), while 4 and 5
        are handle by Python's own string expansion mechanism.
        Note that case 3 is not yet supported by this method!
        """
        self._check_closed()

        if not parameters:
            # Directly execute the statement, nothing else to prepare:
            self._execute_direct(statement)
        else:
            self.executemany(statement, parameters=[parameters])
        return self

    def executemany(self, statement, parameters):
        """Execute statement on database with multiple rows to be inserted/updated
        :param statement: a valid SQL statement
        :param parameters: a nested list/tuple of parameters for multiple rows
        :returns: this cursor
        """
        # First try safer hana-style parameter expansion:
        try:
            statement_id = self.prepare(statement)
        except DatabaseError, msg:
            # Hana expansion failed, check message to be sure of reason:
            if 'incorrect syntax near "%"' not in str(msg):
                # Probably some other error than related to string expansion -> raise an error
                raise
            # Statement contained percentage char, so perform Python style parameter expansion:
            for row_params in parameters:
                operation = format_operation(statement, row_params)
                self._execute_direct(operation)
        else:
            # Continue with Hana style statement execution:
            prepared_statement = self.get_prepared_statement(statement_id)
            self.execute_prepared(prepared_statement, parameters)
        # Return cursor object:
        return self

    def _handle_result_metadata(self, result_metadata):
        description = []
        column_types = []
        for column in result_metadata.columns:
            description.append((column[8], column[1], None, column[3], column[2], None, column[0] & 0b10))

            if column[1] not in by_type_code:
                raise InterfaceError("Unknown column data type: %s" % column[1])
            column_types.append(by_type_code[column[1]])

        return tuple(description), tuple(column_types)

    def _handle_prepared_select(self, parts, result_metadata):

        self.rowcount = -1

        # result metadata
        self.description, self._column_types = self._handle_result_metadata(result_metadata)

        for part in parts:
            if part.kind == part_kinds.RESULTSETID:
                self._resultset_id = part.value
            elif part.kind == part_kinds.RESULTSET:
                # Cleanup buffer
                del self._buffer
                self._buffer = collections.deque()

                for row in self._unpack_rows(part.payload, part.rows):
                    self._buffer.append(row)

                self._received_last_resultset_part = part.attribute & 1
                self._executed = True
            elif part.kind == part_kinds.STATEMENTCONTEXT:
                pass
            else:
                raise InterfaceError("Prepared select statement response, unexpected part kind %d." % part.kind)

    def _handle_prepared_insert(self, parts):
        for part in parts:
            if part.kind == part_kinds.ROWSAFFECTED:
                self.rowcount = part.values[0]
            elif part.kind == part_kinds.TRANSACTIONFLAGS:
                pass
            elif part.kind == part_kinds.STATEMENTCONTEXT:
                pass
            else:
                raise InterfaceError("Prepared insert statement response, unexpected part kind %d." % part.kind)
        self._executed = True

    def _handle_select(self, parts):
        """Handle result from select command"""
        resultset_metadata, resultset_id, statement_context, result_set = parts

        self.rowcount = -1
        self.description, self._column_types = self._handle_result_metadata(resultset_metadata)

        self._resultset_id = resultset_id.value

        # Cleanup buffer
        del self._buffer
        self._buffer = collections.deque()

        for row in self._unpack_rows(result_set.payload, result_set.rows):
            self._buffer.append(row)

        self._received_last_resultset_part = result_set.attribute & 1
        self._executed = True

    def _handle_insert(self, parts):
        self.rowcount = parts[0].values[0]
        self.description = None

    def _unpack_rows(self, payload, rows):
        for _ in iter_range(rows):
            yield tuple(typ.from_resultset(payload, self.connection) for typ in self._column_types)

    def fetchmany(self, size=None):
        self._check_closed()
        if not self._executed:
            raise ProgrammingError("Require execute() first")
        if size is None:
            size = self.arraysize

        _result = []
        _missing = size

        while bool(self._buffer) and _missing > 0:
            _result.append(self._buffer.popleft())
            _missing -= 1

        if _missing == 0 or self._received_last_resultset_part:
            # No rows are missing or there are no additional rows
            return _result

        request = RequestMessage.new(
            self.connection,
            RequestSegment(
                message_types.FETCHNEXT,
                (ResultSetId(self._resultset_id), FetchSize(_missing))
            )
        )
        response = self.connection.send_request(request)

        if response.segments[0].parts[1].attribute & 1:
            self._received_last_resultset_part = True

        resultset_part = response.segments[0].parts[1]
        for row in self._unpack_rows(resultset_part.payload, resultset_part.rows):
            _result.append(row)
        return _result

    def fetchone(self):
        result = self.fetchmany(size=1)
        if result:
            return result[0]
        return None

    def fetchall(self):
        result = self.fetchmany()
        while bool(self._buffer) or not self._received_last_resultset_part:
            result = result + self.fetchmany()
        return result

    def close(self):
        self.connection = None

    def _check_closed(self):
        if self.connection is None or self.connection.closed:
            raise ProgrammingError("Cursor closed")
