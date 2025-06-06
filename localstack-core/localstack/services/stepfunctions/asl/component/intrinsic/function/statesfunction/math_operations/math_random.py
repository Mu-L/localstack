import random
from typing import Any

from localstack.services.stepfunctions.asl.component.intrinsic.argument.argument import (
    ArgumentList,
)
from localstack.services.stepfunctions.asl.component.intrinsic.function.statesfunction.states_function import (
    StatesFunction,
)
from localstack.services.stepfunctions.asl.component.intrinsic.functionname.state_fuinction_name_types import (
    StatesFunctionNameType,
)
from localstack.services.stepfunctions.asl.component.intrinsic.functionname.states_function_name import (
    StatesFunctionName,
)
from localstack.services.stepfunctions.asl.eval.environment import Environment


class MathRandom(StatesFunction):
    # Returns a random number between the specified start and end number.
    #
    # For example:
    # With input
    # {
    #    "start": 1,
    #    "end": 999
    # }
    #
    # Call
    # "random.$": "States.MathRandom($.start, $.end)"
    #
    # Returns
    # {"random": 456 }

    def __init__(self, argument_list: ArgumentList):
        super().__init__(
            states_name=StatesFunctionName(function_type=StatesFunctionNameType.MathRandom),
            argument_list=argument_list,
        )
        if argument_list.size < 2 or argument_list.size > 3:
            raise ValueError(
                f"Expected 2-3 arguments for function type '{type(self)}', but got: '{argument_list}'."
            )

    @staticmethod
    def _validate_integer_value(value: Any, argument_name: str) -> int:
        if not isinstance(value, (int, float)):
            raise TypeError(f"Expected integer value for {argument_name}, but got: '{value}'.")
        # If you specify a non-integer value for the start number or end number argument,
        # Step Functions will round it off to the nearest integer.
        return int(value)

    def _eval_body(self, env: Environment) -> None:
        self.argument_list.eval(env=env)
        args = env.stack.pop()

        seed = None
        if self.argument_list.size == 3:
            seed = args.pop()
            self._validate_integer_value(seed, "seed")

        end = self._validate_integer_value(args.pop(), "end")
        start = self._validate_integer_value(args.pop(), "start")

        rand_gen = random.Random(seed)
        rand_int = rand_gen.randint(start, end)
        env.stack.append(rand_int)
