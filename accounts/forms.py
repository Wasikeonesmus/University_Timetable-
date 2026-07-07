from django import forms
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.contrib.auth.models import User
from .models import UserProfile
from scheduler.models import University, Lecturer, StudentGroup


class LoginForm(AuthenticationForm):
    """Custom login form with styled widgets."""
    username = forms.CharField(
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'Username',
            'autofocus': True,
            'id': 'id_login_username',
        })
    )
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={
            'class': 'form-control',
            'placeholder': 'Password',
            'id': 'id_login_password',
        })
    )


class RegisterForm(UserCreationForm):
    """Registration form — creates a User + UserProfile with auto-linking for lecturers."""
    first_name = forms.CharField(
        max_length=100,
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'First Name', 'id': 'id_reg_first_name'})
    )
    last_name = forms.CharField(
        max_length=100,
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Last Name', 'id': 'id_reg_last_name'})
    )
    email = forms.EmailField(
        widget=forms.EmailInput(attrs={'class': 'form-control', 'placeholder': 'Email', 'id': 'id_reg_email'})
    )
    role = forms.ChoiceField(
        choices=[('student', 'Student'), ('lecturer', 'Lecturer')],
        initial='student',
        widget=forms.Select(attrs={'class': 'form-select', 'id': 'id_reg_role'}),
    )
    university = forms.ModelChoiceField(
        queryset=University.objects.all(),
        required=False,
        widget=forms.Select(attrs={'class': 'form-select', 'id': 'id_reg_university'})
    )

    class Meta:
        model = User
        fields = ['username', 'first_name', 'last_name', 'email', 'password1', 'password2']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['username'].widget.attrs.update({'class': 'form-control', 'placeholder': 'Username', 'id': 'id_reg_username'})
        self.fields['password1'].widget.attrs.update({'class': 'form-control', 'placeholder': 'Password', 'id': 'id_reg_password1'})
        self.fields['password2'].widget.attrs.update({'class': 'form-control', 'placeholder': 'Confirm Password', 'id': 'id_reg_password2'})

    def clean(self):
        cleaned_data = super().clean()
        role = cleaned_data.get('role')
        email = cleaned_data.get('email')
        university = cleaned_data.get('university')

        if role == 'lecturer' and email:
            # Check if lecturer with this email exists in the database
            matching_lecturer = Lecturer.objects.filter(email__iexact=email.strip()).first()
            if not matching_lecturer:
                self.add_error('email', "This email is not registered in the lecturer database. Please ensure it matches your official staff record or contact the administrator.")
            else:
                # Check if this lecturer is already linked to another user account
                existing_link = UserProfile.objects.filter(lecturer=matching_lecturer).first()
                if existing_link:
                    self.add_error('email', f"This lecturer profile is already linked to user account: '{existing_link.user.username}'.")
        
        elif role == 'student':
            if not university:
                self.add_error('university', "University is required for student registration.")

        return cleaned_data

    def save(self, commit=True):
        user = super().save(commit=False)
        user.first_name = self.cleaned_data['first_name']
        user.last_name = self.cleaned_data['last_name']
        user.email = self.cleaned_data['email']
        if commit:
            user.save()
            role = self.cleaned_data['role']
            university = self.cleaned_data.get('university')
            lecturer = None
            
            if role == 'lecturer':
                lecturer = Lecturer.objects.filter(email__iexact=user.email.strip()).first()
                if lecturer:
                    # Adopt the lecturer's university automatically
                    university = lecturer.department.faculty.campus.university
                    
                    # Optional: link the user directly to the lecturer object if that relation is used
                    lecturer.user = user
                    lecturer.save(update_fields=['user'])
            
            UserProfile.objects.create(
                user=user,
                role=role,
                university=university,
                lecturer=lecturer,
            )
        return user


class ProfileForm(forms.ModelForm):
    """Edit profile — role, university, and linked lecturer/student group."""
    first_name = forms.CharField(
        max_length=100,
        widget=forms.TextInput(attrs={'class': 'form-control', 'id': 'id_profile_first_name'})
    )
    last_name = forms.CharField(
        max_length=100,
        widget=forms.TextInput(attrs={'class': 'form-control', 'id': 'id_profile_last_name'})
    )
    email = forms.EmailField(
        widget=forms.EmailInput(attrs={'class': 'form-control', 'id': 'id_profile_email'})
    )

    def __init__(self, *args, **kwargs):
        is_admin = kwargs.pop('is_admin', False)
        user_role = kwargs.pop('user_role', None)
        super().__init__(*args, **kwargs)
        
        if not is_admin:
            # Role and university remain admin-controlled
            self.fields['role'].disabled = True
            self.fields['role'].widget.attrs['readonly'] = True
            self.fields['university'].disabled = True
            self.fields['university'].widget.attrs['readonly'] = True
            
            # Allow lecturers to self-select from unassigned lecturer records
            if user_role == 'lecturer':
                # Filter to lecturers not already assigned to another user
                assigned_lecturer_ids = UserProfile.objects.filter(
                    lecturer__isnull=False
                ).exclude(id=self.instance.id).values_list('lecturer_id', flat=True)
                self.fields['lecturer'].queryset = self.fields['lecturer'].queryset.exclude(
                    id__in=assigned_lecturer_ids
                )
                self.fields['lecturer'].required = True
                self.fields['student_group'].disabled = True
                self.fields['student_group'].widget.attrs['readonly'] = True
            
            # Allow students to self-select from unassigned student groups
            elif user_role == 'student':
                # Filter to student groups not already assigned to another user
                assigned_group_ids = UserProfile.objects.filter(
                    student_group__isnull=False
                ).exclude(id=self.instance.id).values_list('student_group_id', flat=True)
                self.fields['student_group'].queryset = self.fields['student_group'].queryset.exclude(
                    id__in=assigned_group_ids
                )
                self.fields['student_group'].required = True
                self.fields['lecturer'].disabled = True
                self.fields['lecturer'].widget.attrs['readonly'] = True
            
            # Admins/schedulers can't self-assign
            else:
                self.fields['lecturer'].disabled = True
                self.fields['lecturer'].widget.attrs['readonly'] = True
                self.fields['student_group'].disabled = True
                self.fields['student_group'].widget.attrs['readonly'] = True

    class Meta:
        model = UserProfile
        fields = ['role', 'university', 'lecturer', 'student_group', 'bio']
        widgets = {
            'role': forms.Select(attrs={'class': 'form-select', 'id': 'id_profile_role'}),
            'university': forms.Select(attrs={'class': 'form-select', 'id': 'id_profile_university'}),
            'lecturer': forms.Select(attrs={'class': 'form-select', 'id': 'id_profile_lecturer'}),
            'student_group': forms.Select(attrs={'class': 'form-select', 'id': 'id_profile_student_group'}),
            'bio': forms.Textarea(attrs={'class': 'form-control', 'rows': 3, 'id': 'id_profile_bio'}),
        }
