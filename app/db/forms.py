from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User


class LoginForm(forms.Form):
    username = forms.CharField(
                label="",
                widget=forms.TextInput(
                    attrs={
                        'placeholder': 'Nombre de usuario' ,
                        'autofocus': 'autofocus' ,
                        'class': 'input_login',
                    })
                )
    password = forms.CharField(
                label="",
                widget=forms.PasswordInput(
                    attrs={
                    'placeholder': 'Contraseña', 
                    'value': '',
                    'class': 'input_login',
                    })
                )