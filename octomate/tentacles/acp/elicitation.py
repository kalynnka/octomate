"""Translate ACP form fields into question cards and typed response content."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from acp import schema
from pydantic import UUID7, Field, JsonValue, TypeAdapter, ValidationError

from octomate.schemas.deferred import (
    MAX_QUESTION_CHOICES,
    DeferredActionBatch,
    QuestionAnswer,
    QuestionRequest,
)
from octomate.types.json import JsonObject

type FormProperty = (
    schema.ElicitationStringPropertySchema
    | schema.ElicitationNumberPropertySchema
    | schema.ElicitationIntegerPropertySchema
    | schema.ElicitationBooleanPropertySchema
    | schema.ElicitationMultiSelectPropertySchema
)


class AcpFormValidationError(ValueError):
    """Field-specific feedback that leaves the live form open for correction."""

    errors: dict[UUID7, str]

    def __init__(self, errors: dict[UUID7, str]) -> None:
        super().__init__("Correct the highlighted fields before submitting.")
        self.errors = errors


@dataclass(frozen=True)
class AcpForm:
    """One session-scoped ACP form, preserving property names and enum values."""

    request: schema.CreateFormSessionElicitationRequest
    agent_name: str = "ACP agent"  # Identity shown on every form card.

    @staticmethod
    def choices(prop: FormProperty) -> dict[str, str]:
        """Map visible labels to wire values without replacing provider values."""
        options: list[schema.EnumOption] = []
        match prop:
            case schema.ElicitationStringPropertySchema():
                if prop.enum is not None:
                    return {value: value for value in prop.enum}
                options = prop.one_of or []
            case schema.ElicitationMultiSelectPropertySchema():
                if isinstance(prop.items, schema.UntitledMultiSelectItems):
                    return {value: value for value in prop.items.enum}
                options = prop.items.any_of
        titles = [option.title for option in options]
        return {
            option.title
            if titles.count(option.title) == 1
            else f"{option.title} ({option.const})": option.const
            for option in options
        }

    def questions(self) -> list[QuestionRequest]:
        questions: list[QuestionRequest] = []
        required = self.request.requested_schema.required or []
        for name, prop in (self.request.requested_schema.properties or {}).items():
            choices = list(self.choices(prop))
            if isinstance(prop, schema.ElicitationBooleanPropertySchema):
                choices = ["Yes", "No"]
            hint = "\n".join(
                filter(
                    None,
                    [f"{self.agent_name}: {self.request.message}", prop.description],
                )
            )
            if len(choices) > MAX_QUESTION_CHOICES:
                hint += "\nAll choices: " + "; ".join(choices)
            if name not in required:
                hint += "\nOptional; submit an empty answer to omit this field."
            questions.append(
                QuestionRequest(
                    question=prop.title or name,
                    choices=choices[:MAX_QUESTION_CHOICES] or None,
                    hint=hint,
                    multi_select=isinstance(
                        prop, schema.ElicitationMultiSelectPropertySchema
                    ),
                )
            )
        questions.append(
            QuestionRequest(
                question="Submit this form?",
                choices=["Save draft", "Submit", "Decline", "Cancel"],
                hint=f"{self.agent_name}: {self.request.message}"
                + "\nReview your answers before submitting. Decline or cancel to close without sending them.",
            )
        )
        return questions

    def decision(self, batch: DeferredActionBatch) -> str | None:
        """The explicit form control is separate from provider-owned fields."""
        position = len(self.request.requested_schema.properties or {})
        return next(
            (
                action.result
                for action in batch.questions
                if action.position == position and isinstance(action.result, str)
            ),
            None,
        )

    @classmethod
    def value(
        cls, prop: FormProperty, answer: QuestionAnswer
    ) -> str | int | float | bool | list[str]:
        """Coerce text input at the boundary and enforce the form's constraints."""
        choices = cls.choices(prop)
        if isinstance(prop, schema.ElicitationMultiSelectPropertySchema):
            selected = answer if isinstance(answer, list) else [answer]
            values = [choices.get(value, value) for value in selected]
            if any(value not in choices.values() for value in values):
                raise ValueError("Answer is not one of the form's choices")
            return TypeAdapter(
                Annotated[
                    list[str],
                    Field(min_length=prop.min_items, max_length=prop.max_items),
                ]
            ).validate_python(values)
        if not isinstance(answer, str):
            raise ValueError("This form field requires a single answer")
        match prop:
            case schema.ElicitationStringPropertySchema():
                value = choices.get(answer, answer)
                if choices and value not in choices.values():
                    raise ValueError("Answer is not one of the form's choices")
                return TypeAdapter(
                    Annotated[
                        str,
                        Field(
                            min_length=prop.min_length,
                            max_length=prop.max_length,
                            pattern=prop.pattern,
                        ),
                    ]
                ).validate_python(value)
            case schema.ElicitationBooleanPropertySchema():
                return TypeAdapter(bool).validate_python(answer)
            case schema.ElicitationIntegerPropertySchema():
                return TypeAdapter(
                    Annotated[int, Field(ge=prop.minimum, le=prop.maximum)]
                ).validate_python(answer)
            case schema.ElicitationNumberPropertySchema():
                return TypeAdapter(
                    Annotated[
                        float,
                        Field(ge=prop.minimum, le=prop.maximum, allow_inf_nan=False),
                    ]
                ).validate_python(answer)

    def content(self, batch: DeferredActionBatch) -> JsonObject:
        """Reassemble fields by their stable position, omitting blank optional input."""
        properties = self.request.requested_schema.properties or {}
        required = self.request.requested_schema.required or []
        content: JsonObject = {}
        errors: dict[UUID7, str] = {}
        for (name, prop), action in zip(
            properties.items(),
            sorted(batch.questions, key=lambda action: action.position)[
                : len(properties)
            ],
            strict=True,
        ):
            if name not in required and action.result in (None, "", []):
                continue
            if not isinstance(action.result, str | list):
                errors[action.id] = "An answer is required."
                continue
            try:
                value = self.value(prop, action.result)
                content[name] = (
                    list[JsonValue](value) if isinstance(value, list) else value
                )
            except ValidationError as error:
                errors[action.id] = str(error.errors(include_input=False)[0]["msg"])
            except ValueError as error:
                errors[action.id] = str(error)
        if errors:
            raise AcpFormValidationError(errors)
        return content
