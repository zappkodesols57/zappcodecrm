from rest_framework import viewsets, status, views, generics
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.decorators import action
from rest_framework_simplejwt.tokens import RefreshToken
from django.contrib.auth import authenticate
from django.db.models import Count, Sum
from django.utils import timezone

from accounts.models import User
from leads.models import Lead
from followups.models import FollowUp
from .serializers import UserSerializer, LeadSerializer, FollowUpSerializer

# --- AUTHENTICATION ENDPOINTS ---

class LoginAPIView(views.APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        phone = request.data.get('phone')
        password = request.data.get('password')
        
        if not phone or not password:
            return Response({'error': 'Phone and password are required.'}, status=status.HTTP_400_BAD_REQUEST)
            
        try:
            # Look up user by phone. Depending on your system, phone might not be strictly unique,
            # but we assume the first active user matching this phone is the intended one.
            user_obj = User.objects.filter(phone=phone, is_active=True).first()
            if not user_obj:
                return Response({'error': 'No active account found with this phone number.'}, status=status.HTTP_404_NOT_FOUND)
                
            # Authenticate using the username found for this phone
            user = authenticate(username=user_obj.username, password=password)
            if user:
                refresh = RefreshToken.for_user(user)
                return Response({
                    'access_token': str(refresh.access_token),
                    'refresh_token': str(refresh),
                    'user': UserSerializer(user).data
                })
            else:
                return Response({'error': 'Invalid credentials.'}, status=status.HTTP_401_UNAUTHORIZED)
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class VerifyOTPAPIView(views.APIView):
    permission_classes = [AllowAny]
    
    def post(self, request):
        phone = request.data.get('phone')
        otp = request.data.get('otp')
        # Placeholder for actual OTP verification logic
        if otp == "1234":  # Dummy check
            user = User.objects.filter(phone=phone, is_active=True).first()
            if user:
                refresh = RefreshToken.for_user(user)
                return Response({
                    'access_token': str(refresh.access_token),
                    'refresh_token': str(refresh),
                    'user': UserSerializer(user).data
                })
        return Response({'error': 'Invalid OTP'}, status=status.HTTP_400_BAD_REQUEST)

class ForgotPasswordAPIView(views.APIView):
    permission_classes = [AllowAny]
    def post(self, request):
        phone = request.data.get('phone')
        # Placeholder logic to send reset link/OTP
        return Response({'message': 'If an account exists, a reset link/OTP has been sent.'})

class LogoutAPIView(views.APIView):
    permission_classes = [IsAuthenticated]
    def post(self, request):
        # In stateless JWT, logout is often handled client-side by deleting the token.
        # For true server-side logout, we'd blacklist the refresh token.
        return Response({'message': 'Logged out successfully.'})

class UserProfileAPIView(generics.RetrieveAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = UserSerializer

    def get_object(self):
        return self.request.user

# --- MAIN MODULE ENDPOINTS ---

class LeadViewSet(viewsets.ModelViewSet):
    serializer_class = LeadSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        """Enforce strict row-level multi-tenancy based on the user's hospital."""
        user = self.request.user
        qs = Lead.objects.filter(is_archived=False)
        
        # Enforce multi-tenancy
        if user.hospital:
            qs = qs.filter(hospital=user.hospital)
            
        # Role-based filtering
        if user.role in ['COUNSELLOR', 'HR', 'DOCTOR']:
            qs = qs.filter(assigned_to=user)
            
        # Standard query param filters
        status_param = self.request.query_params.get('status')
        if status_param:
            qs = qs.filter(deal_status=status_param)
            
        search = self.request.query_params.get('search')
        if search:
            qs = qs.filter(name__icontains=search)
            
        return qs.order_by('-created_at')

    def perform_create(self, serializer):
        # Automatically assign the hospital of the logged-in user to the new lead
        serializer.save(hospital=self.request.user.hospital, created_by=self.request.user)

    @action(detail=True, methods=['patch'], url_path='status')
    def update_status(self, request, pk=None):
        lead = self.get_object()
        new_status = request.data.get('status')
        if new_status:
            lead.deal_status = new_status
            lead.save()
            return Response(self.get_serializer(lead).data)
        return Response({'error': 'Status is required.'}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'], url_path='call-outcome')
    def call_outcome(self, request, pk=None):
        lead = self.get_object()
        # Create a followup/note based on call outcome
        notes = request.data.get('remark1', '')
        call_result = request.data.get('call_result', '')
        
        FollowUp.objects.create(
            lead=lead,
            assigned_to=request.user,
            followup_type='CALL',
            notes=f"Outcome: {call_result} | Notes: {notes}",
            actual_date=timezone.localdate(),
            status='COMPLETED'
        )
        return Response({'message': 'Call outcome recorded successfully.'})

    @action(detail=True, methods=['post'], url_path='assign-doctor')
    def assign_doctor(self, request, pk=None):
        lead = self.get_object()
        doctor_id = request.data.get('doctor_id')
        try:
            doctor = User.objects.get(id=doctor_id, role='DOCTOR')
            # Assign the doctor in the JSON field or a dedicated field if one exists
            nelson_data = lead.nelson_data or {}
            nelson_data['doctor_id'] = doctor.id
            nelson_data['doctor_name'] = doctor.get_full_name()
            lead.nelson_data = nelson_data
            
            # Alternatively, re-assign the lead to the doctor directly:
            # lead.assigned_to = doctor
            
            lead.save()
            return Response(self.get_serializer(lead).data)
        except User.DoesNotExist:
            return Response({'error': 'Doctor not found.'}, status=status.HTTP_404_NOT_FOUND)

class DashboardAnalyticsAPIView(views.APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        qs = Lead.objects.filter(is_archived=False)
        if user.hospital:
            qs = qs.filter(hospital=user.hospital)
            
        total_leads = qs.count()
        appointments = qs.filter(nelson_data__appo_book__iexact='YES').count()
        
        return Response({
            'total_leads': total_leads,
            'appointments_booked': appointments,
            'conversion_rate': round(appointments / total_leads, 2) if total_leads > 0 else 0
        })

class DoctorListAPIView(generics.ListAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = UserSerializer

    def get_queryset(self):
        qs = User.objects.filter(role='DOCTOR', is_active=True)
        if self.request.user.hospital:
            qs = qs.filter(hospital=self.request.user.hospital)
        return qs

class FollowUpViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = FollowUpSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = FollowUp.objects.all()
        if self.request.user.hospital:
            qs = qs.filter(lead__hospital=self.request.user.hospital)
        
        date_param = self.request.query_params.get('date')
        if date_param:
            qs = qs.filter(scheduled_date=date_param)
            
        return qs.order_by('-scheduled_date', '-scheduled_time')

class AppointmentListAPIView(generics.ListAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = LeadSerializer

    def get_queryset(self):
        user = self.request.user
        qs = Lead.objects.filter(is_archived=False, nelson_data__appo_book__iexact='YES')
        
        if user.hospital:
            qs = qs.filter(hospital=user.hospital)
            
        if user.role in ['COUNSELLOR', 'HR', 'DOCTOR']:
            qs = qs.filter(assigned_to=user)
            
        return qs.order_by('-created_at')

