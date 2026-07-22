from django import forms
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.contrib.auth.models import User
from .models import UserProfile
from scheduler.models import University, Lecturer, StudentGroup


class LoginForm(AuthenticationForm):
    """Custom login form with styled widgets."""
    username = forms.CharField(
        label="Email Address",
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'Enter your email address (e.g. wasikeonesmus980@gmail.com)',
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
    student_group = forms.ModelChoiceField(
        queryset=StudentGroup.objects.all(),
        required=False,
        label="Student Group / Course",
        widget=forms.Select(attrs={'class': 'form-select', 'id': 'id_reg_student_group'})
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
        student_group = cleaned_data.get('student_group')

        if email and not cleaned_data.get('username'):
            base_un = email.split('@')[0]
            un = base_un
            c = 1
            while User.objects.filter(username=un).exists():
                un = f"{base_un}{c}"
                c += 1
            cleaned_data['username'] = un
            self.cleaned_data['username'] = un

        if role == 'lecturer' and email:
            # Check if lecturer profile with this email is already linked to another user account
            matching_lecturer = Lecturer.objects.filter(email__iexact=email.strip()).first()
            if matching_lecturer:
                existing_link = UserProfile.objects.filter(lecturer=matching_lecturer).first()
                if existing_link and existing_link.user.email.strip().lower() != email.strip().lower():
                    self.add_error('email', f"This lecturer profile is already linked to user account: '{existing_link.user.username}'.")

        elif role == 'student':
            if not university:
                self.add_error('university', "University is required for student registration.")
            if 'student_group' in self.data and not student_group:
                self.add_error('student_group', "Student Group / Course is required for student registration.")
            elif university and student_group and student_group.program.department.faculty.campus.university != university:
                self.add_error('student_group', "Selected student group does not belong to the chosen university.")

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
            student_group = self.cleaned_data.get('student_group')
            lecturer = None
            
            if role == 'lecturer':
                lecturer = Lecturer.objects.filter(email__iexact=user.email.strip()).first()
                if lecturer:
                    # Adopt the lecturer's university automatically if present
                    if lecturer.department and lecturer.department.faculty and lecturer.department.faculty.campus:
                        university = lecturer.department.faculty.campus.university
                    lecturer.user = user
                    lecturer.is_verified = True
                    lecturer.save(update_fields=['user', 'is_verified'])
                else:
                    # Auto-create Lecturer profile if not pre-existing
                    from scheduler.models import Department
                    dept = None
                    if university:
                        dept = Department.objects.filter(faculty__campus__university=university).first()
                    if not dept:
                        dept = Department.objects.first()

                    full_name = f"{user.first_name} {user.last_name}".strip() or user.username
                    lecturer = Lecturer.objects.create(
                        name=full_name,
                        email=user.email.strip().lower(),
                        department=dept,
                        user=user,
                        is_verified=True
                    )
                    if dept and dept.faculty and dept.faculty.campus:
                        university = dept.faculty.campus.university
            
            profile, created = UserProfile.objects.get_or_create(
                user=user,
                defaults={
                    'role': role,
                    'university': university,
                    'lecturer': lecturer,
                    'student_group': student_group if role == 'student' else None,
                }
            )
            if not created:
                profile.role = role
                if university:
                    profile.university = university
                if lecturer:
                    profile.lecturer = lecturer
                if role == 'student' and student_group:
                    profile.student_group = student_group
                profile.save()
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
