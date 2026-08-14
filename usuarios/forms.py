from django.contrib.auth.forms import AuthenticationForm


_INPUT = "form-control"


class ScoreSyncAuthForm(AuthenticationForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", _INPUT)
